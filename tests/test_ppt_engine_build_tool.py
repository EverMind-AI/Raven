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

from raven.contracts.tool import ToolResult
from raven_ppt.backends.script import script_path
from raven_ppt.contracts import (
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
from raven_ppt.profiles import registry
from raven_ppt.stages.build import BATCH_VIEWS, CATCH_UP_VIEWS, _showing
from raven_ppt.tools.build import PptBuildTool
from raven_ppt.tools.review import RECORD_FILE, _already_read, _marked
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401


class FakeViews:
    def __init__(self, pages: int = 2) -> None:
        self.pages_count = pages
        self.asked: list[list[int]] = []
        self.labels: list[str | None] = []

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

    def data_uri(self, png: Path, budget: int | None = None, label: str | None = None) -> str:
        self.labels.append(label)
        return f"data:image/png;base64,{png.stem}"


class FakeStage:
    def __init__(self, result: StageResult, views_per_call: int = BATCH_VIEWS) -> None:
        self.result = result
        self.calls: list[str | None] = []
        self.drafts: list[bool] = []
        self.releases: list[bool] = []
        self.slides: list[list[int] | None] = []
        self.backend = None
        self.measure = None
        self.profile = registry.get("script_author")
        self.destination = lambda p: p.exports_dir / "deck.pptx"
        # The number the tool reads its `slides` cap off, and the one the real stage
        # selects `showing` with. A fake holding its own copy of that rule is the drift
        # this fixture exists to catch, so it delegates to `_showing`.
        self.views_per_call = views_per_call
        self.catch_up_views = CATCH_UP_VIEWS

    async def run(
        self,
        project: Project,
        script: str | None = None,
        *,
        slides=None,
        page_from: int = 1,
        draft: bool = False,
        release: bool = False,
    ) -> StageResult:
        self.calls.append(script)
        self.drafts.append(draft)
        self.releases.append(release)
        self.slides.append(list(slides) if slides is not None else None)
        # The stage decides which pages the reply shows, because that is where the
        # deck is published and an unseen page has to refuse before delivery.
        pages = getattr(self.result.data.get("outcome"), "pages", 0)
        showing = _showing(pages, slides, page_from, self.views_per_call)
        return replace(self.result, data={**self.result.data, "showing": showing})


def _ok(project: Project, findings=(), pages: int = 2, sources=None, **data) -> StageResult:
    deck = project.build_dir / "deck.pptx"
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"PK")
    outcome = BuildOutcome(
        ok=True,
        pptx_path=deck,
        pages=pages,
        sources=sources if sources is not None else (PageSource(page=1, first_line=0, last_line=5),),
    )
    blocking = [f for f in findings if f.severity is Severity.BLOCKING]
    return StageResult(
        ok=not blocking,
        findings=tuple(findings),
        data={"outcome": outcome, "pptx_path": str(project.exports_dir / "deck.pptx"), **data},
    )


def _tool(
    project: Project,
    result: StageResult,
    views: FakeViews | None = None,
    views_per_call: int = BATCH_VIEWS,
) -> PptBuildTool:
    return PptBuildTool(
        workspace=project.workspace,
        stage=FakeStage(result, views_per_call),  # type: ignore[arg-type]
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
    write_outline(
        Outline(
            takeaway="it works",
            pages=(
                PagePlan(
                    page=1,
                    claim="It works",
                    layout="P14",
                    layers=("M4", "M11"),
                    anti_pattern="a lane that would read the same with the chart removed",
                ),
            ),
        ),
        outline_path(deck),
    )
    return deck


@pytest.mark.asyncio
async def test_a_restored_helper_is_named_on_a_failed_build(project: Project) -> None:
    """The livelock this note exists for was nine failed builds in a row.

    `run_script` carries the restore note out of every post-provision exit, and
    the tool then read `outcome.note` only after its success return -- so the one
    case that mattered, a helper restored and the script then dying, stayed as
    silent as before. Driven through `ppt_build` because that is the surface the
    author reads.
    """
    outcome = BuildOutcome(ok=False, stderr="Traceback (most recent call last): ...", note="themes.json restored")
    body = _body(await _tool(project, StageResult(ok=False, data={"outcome": outcome})).execute(project="tarvis"))

    assert body["ok"] is False
    assert "themes.json" in (body.get("note") or ""), "a failed build does not name the helper it put back"


@pytest.mark.asyncio
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
    blocks = reply.blocks or []
    # One label per render and strictly alternating, which is what keeps a label with
    # its picture on a transport that moves the images to a following message.
    assert [block["type"] for block in blocks] == ["text", "image_url", "text", "image_url"]
    # A clean page carries its claim on the label line and nothing more: the whole
    # plan used to ride with every page on every build, and twenty-three builds of
    # one deck repeated the same twenty plans into the transcript.
    assert blocks[0]["text"] == "Page 1 of 2: It works"
    assert "Page 2 of 2" in blocks[2]["text"]


@pytest.mark.asyncio
async def test_a_page_with_something_to_fix_gets_its_whole_plan_back(project: Project) -> None:
    """The structure and the way it goes wrong travel with the render of a page that
    has a finding. Both were written by the plan and read by nothing, so a page could
    declare `P14 + M4 + M11` and the author looking at it be told only what the page
    claims."""
    finding = Finding(kind="card_overflow", severity=Severity.WARNING, message="a card runs past its edge", page=1)
    reply = await _tool(project, _ok(project, [finding])).execute(project="tarvis", slides=[1, 2])

    assert isinstance(reply, ToolResult)
    blocks = reply.blocks or []
    assert "Planned claim: It works" in blocks[0]["text"]
    assert "Planned structure: P14 + M4 + M11" in blocks[0]["text"]
    assert "Must not have become: a lane that would read the same" in blocks[0]["text"]
    assert "a card runs past its edge" in blocks[0]["text"]
    assert blocks[2]["text"] == "Page 2 of 2", "a page the outline does not plan keeps the bare label"


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
    assert "look at every page below" in body["next_step"]
    assert "pages_not_shown" not in body, "a two-page deck fits in one batch"


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
    """Every page named, in order, and nothing else -- the surplus used to be dropped.

    `slides=[3, 1, 2]` is three pages inside the cap, and one render came back: the
    tool's own cap said one page and the caller was told nothing about the other two,
    which is how a model concludes it has looked at a page nobody rendered.
    """
    views = FakeViews(pages=18)
    tool = _tool(project, _ok(project, pages=18), views)
    await tool.execute(project="tarvis", slides=[3, 1, 2])
    assert views.asked == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_the_first_batch_comes_back_unasked(project: Project) -> None:
    """Omitting `slides` walks the deck a batch at a time, not a page at a time.

    `execute` used to pass `slides=[page_from]`, which made the stage's walk branch
    unreachable: an eighteen-page deck took eighteen builds to look at once.
    """
    views = FakeViews(pages=18)
    stage_views = FakeViews(pages=18)
    await _tool(project, _ok(project, pages=18), views).execute(project="tarvis")
    assert views.asked == [[1, 2, 3]]

    tool = _tool(project, _ok(project, pages=18), stage_views)
    await tool.execute(project="tarvis", page_from=7)
    assert stage_views.asked == [[7, 8, 9]]
    assert tool.stage.slides == [None], "the walk branch only runs when slides is unset"


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


async def test_a_draft_is_told_where_the_second_reading_is(project: Project) -> None:
    """A run stayed in draft for fourteen builds and forty iterations. The pointer at
    `ppt_review` was in the delivered-and-clean branch alone, so a deck that never
    leaves draft is a deck whose author is never told a second reading exists -- and
    the draft branch is exactly where an author sits while it still believes the pages
    need work.
    """
    # No `pptx_path`, which is what the stage really hands back for a draft: it returns
    # before publication. With one in the data this passed while the text it asserts was
    # unreachable in production -- the branch carrying it tests for a published path.
    built = _ok(project)
    drafted = replace(built, data={key: value for key, value in built.data.items() if key != "pptx_path"})
    tool = _tool(project, drafted)

    reply = await tool.execute(project=project.slug, draft=True)
    said = reply if isinstance(reply, str) else reply.model_text

    assert "without `draft`" in said, "the draft still has to be told how to deliver"
    assert "ppt_review" in said


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


@pytest.mark.parametrize("budget", [1, 2, 3, 5, 12])
@pytest.mark.asyncio
async def test_the_slides_cap_is_exactly_the_number_of_renders_the_reply_carries(project: Project, budget: int) -> None:
    """The invariant the stage's comment claims: one number, not two that agree.

    Two constants carried this and drifted to 1 and 3. Tighter on the tool side is not
    the safe direction it looks like: the cap is what tells a caller how many pages it
    may name, so a cap under the batch forbids pages the reply would have carried, and
    a cap over it lets a call name pages the reply drops without saying so. Either way
    a model concludes it has looked at a page nobody rendered.
    """
    views = FakeViews(pages=40)
    tool = _tool(project, _ok(project, pages=40), views, views_per_call=budget)
    cap = tool.parameters["properties"]["slides"]["maxItems"]

    assert cap == budget
    assert cap == tool.stage.views_per_call, "the tool must read the number, not restate it"

    asked = list(range(3, 3 + cap))
    reply = await tool.execute(project="tarvis", slides=asked)

    assert views.asked == [asked], "every page the cap allows comes back"
    labels = [b["text"] for b in (reply.blocks or []) if b["type"] == "text"]
    assert [f"Page {n} of 40" in label for n, label in zip(asked, labels, strict=True)] == [True] * cap


@pytest.mark.parametrize("budget", [1, 3, 5])
@pytest.mark.asyncio
async def test_the_walk_advances_by_the_same_number_it_shows(project: Project, budget: int) -> None:
    """Omitting `slides` walks the deck in batches of the budget, and the reply's own
    `page_from` hint lands on the page after the last one it showed -- so following it
    covers the deck exactly once, with no page skipped and none shown twice."""
    seen_pages: list[int] = []
    page_from, calls = 1, 0
    while page_from <= 19:
        views = FakeViews(pages=19)
        body = _body(
            await _tool(project, _ok(project, pages=19), views, views_per_call=budget).execute(
                project="tarvis", page_from=page_from
            )
        )
        shown = views.asked[0]
        assert len(shown) <= budget
        seen_pages += shown
        calls += 1
        page_from = shown[-1] + 1
        if page_from <= 19:
            assert body["pages_not_shown"] == 19 - shown[-1]
            assert f"page_from={page_from}" in body["next_step"]

    assert seen_pages == list(range(1, 20)), "the deck is covered once, in order"
    assert calls == -(-19 // budget), "no more calls than the budget makes necessary"


@pytest.mark.asyncio
async def test_a_batch_of_renders_carries_page_identity_inside_each_picture(project: Project) -> None:
    """The fallback that moves the pictures to a following user message.

    The fork's loop moved each render's label line along with it (its
    ``labelled_images``); the trunk loop's demotion moves the pictures alone, so
    the page number is painted onto the render itself at encode time (D3): the
    label rides ``data_uri(..., label=...)`` and this pins that every page in a
    batch is stamped with its own number while the adjacent text keeps the plan.
    """
    views = FakeViews(pages=19)
    reply = await _tool(project, _ok(project, pages=19), views).execute(project="tarvis")
    blocks = reply.blocks or []

    assert [b["type"] for b in blocks] == ["text", "image_url"] * 3
    for number, label in zip((1, 2, 3), blocks[::2], strict=True):
        assert f"Page {number} of 19" in label["text"]
    assert views.labels == ["page 1", "page 2", "page 3"]


def _real_stage(pages: int, views_per_call: int) -> Any:
    """A real `BuildStage`, so the unseen record is the one publication reads."""
    from raven_ppt.backends.script import script_path
    from raven_ppt.stages.build import BuildStage

    async def backend(project: Project, _script: str | None) -> BuildOutcome:
        deck = project.build_dir / "deck.pptx"
        deck.parent.mkdir(parents=True, exist_ok=True)
        deck.write_bytes(b"PK")
        script_path(project).parent.mkdir(parents=True, exist_ok=True)
        script_path(project).write_text(
            "".join(f"# SLIDE {n}\ndraw_{n}()\n" for n in range(1, pages + 1)), encoding="utf-8"
        )
        return BuildOutcome(
            ok=True,
            pptx_path=deck,
            pages=pages,
            sources=tuple(PageSource(page=n, first_line=2 * (n - 1), last_line=2 * n) for n in range(1, pages + 1)),
            source_digest="x",
        )

    async def measure(
        _project: Project, _pptx: Path, _outcome: Any = None, _changed: str = "delivery"
    ) -> list[Finding]:
        return []

    return BuildStage(
        backend=backend,
        measure=measure,
        profile=registry.get("script_author"),
        views_per_call=views_per_call,
    )


@pytest.mark.asyncio
async def test_the_unseen_gate_records_exactly_the_pages_the_reply_rendered(project: Project) -> None:
    """What blocks publication is computed from the same list the pictures come from.

    This is the invariant the stage's comment asserts and the code did not hold. The
    gate is written from the pages the stage selected and the reply renders that same
    selection, so the two must be one list -- a gate reading a wider number would mark
    a page seen that was never shown, which is the one refusal a model cannot answer by
    editing the deck.

    Both halves are asserted, because only one of them is about the drift: that the two
    agree, and that they agree on the whole batch the stage selects -- the unseen pages,
    up to the catch-up number -- rather than on the single page the tool's own cap used
    to force.
    """
    from raven_ppt.services import seen

    views = FakeViews(pages=10)
    stage = _real_stage(pages=10, views_per_call=3)
    tool = PptBuildTool(workspace=project.workspace, stage=stage, views=views, profile=registry.get("script_author"))

    body = _body(await tool.execute(project="tarvis"))
    recorded = set(json.loads(seen.seen_path(project).read_text())["pages"])

    assert views.asked == [[1, 2, 3, 4, 5, 6]], "the unseen pages, as many as the catch-up batch carries"
    assert recorded == {"1", "2", "3", "4", "5", "6"}, "the gate records what was shown, and all of it"
    assert body["ok"] is False and "unseen_page" in body["error"]
    unseen = [f for f in body["measured"]["for_you"] if f["kind"] == "unseen_page"]
    assert unseen[0]["detail"]["pages"] == [7, 8, 9, 10], "the four it did not show"
    assert body["pages_not_yet_shown"] == [7, 8, 9, 10] and body["pages_not_shown"] == 4
    assert "without slides" in body["next_step"]

    # And the second build keeps them equal: it comes back with exactly the unseen rest.
    await tool.execute(project="tarvis", page_from=4)
    assert views.asked[-1] == [7, 8, 9, 10]
    assert set(json.loads(seen.seen_path(project).read_text())["pages"]) == {str(n) for n in range(1, 11)}


def _body(reply) -> dict:
    """The reply's JSON, whether or not it came back with pictures attached."""
    said = reply if isinstance(reply, str) else reply.model_text
    return json.loads(said[said.index("{") : said.rindex("}") + 1])


class _Reader:
    """A stand-in for ppt_review that records when the build reached for it."""

    name = "ppt_review"

    def __init__(self, reply=None, deck: Project | None = None, covers=None) -> None:
        self.calls: list[str] = []
        self.asked: list[list[int] | None] = []
        self.deck = deck
        # None means "whatever it was asked for", which is what the real tool covers.
        # A fixed set stands in for the cap: one call reads MAX_PAGES of a longer deck.
        self.covers = covers
        self._reply = (
            reply
            if reply is not None
            else (
                '{"ok": true, "project": "ws", "pages_reviewed": 3, "pages_with_something_to_fix": 1,'
                ' "problems": {"2": [{"kind": "underfilled_page", "where": "the lower third",'
                ' "what": "empty", "fix": "say more"}]}, "next_step": "take them one page at a time"}'
            )
        )

    async def execute(self, project: str, pages=None, **_kwargs):
        self.calls.append(project)
        self.asked.append(list(pages) if pages is not None else None)
        if self.deck is not None:
            record = self.deck.review_dir / RECORD_FILE
            record.parent.mkdir(parents=True, exist_ok=True)
            # `pages_read` and not an empty object: the hook runs while a page-version of
            # the deck is uncovered, so the pages this round covered have to be in it.
            # Written through the tool's own `_marked` -- the union, at the version each
            # page is at -- because a fake that wrote a record of its own would hide
            # exactly the defects this file pins.
            covered = list(pages or ()) if self.covers is None else list(self.covers)
            record.write_text(json.dumps({"pages_read": _marked(self.deck, covered)}), encoding="utf-8")
        return ToolResult(model_text=self._reply, blocks=[{"type": "text", "text": "Page 2, and what"}])


def _versioned(project: Project, pages: int, *, rewritten: int | None = None) -> tuple[PageSource, ...]:
    """A build program of one line per page, so a page's fingerprint is its own line.

    That is what makes a rewrite expressible here: `rewritten=4` leaves every other
    page's code byte-identical, which is the case the per-page-version record exists
    for.
    """
    lines = [f"page({number})\n" for number in range(1, pages + 1)]
    if rewritten is not None:
        lines[rewritten - 1] = f"page({rewritten}, again)\n"
    path = script_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    # The reading's version is what the build rendered: a page's pixels stand in for
    # its line here, so `rewritten=4` changes page 4's render and nobody else's.
    project.review_dir.mkdir(parents=True, exist_ok=True)
    for number, line in enumerate(lines, start=1):
        (project.review_dir / f"page-{number:03d}.png").write_bytes(b"\x89PNG" + line.encode("utf-8"))
    return tuple(PageSource(page=number, first_line=number - 1, last_line=number) for number in range(1, pages + 1))


async def test_a_delivered_deck_is_read_without_being_asked(project: Project) -> None:
    """Asking the author to call it left it to the author. One run made twenty builds
    and reached `ppt_review` at iteration 61 on its own; a run that never leaves draft
    never meets the sentence naming it at all.
    """
    reader = _Reader()
    tool = _tool(project, _ok(project))
    tool.review = reader

    reply = await tool.execute(project=project.slug)
    said = reply if isinstance(reply, str) else reply.model_text

    assert reader.calls == [project.slug]
    assert reader.asked == [[1, 2]], "the pages nobody has read, which here is the deck"
    assert "first_reading" in said
    assert "underfilled_page" in said
    assert "take them one page at a time" in said


async def test_a_draft_is_read_once_five_of_its_pages_are_unread(project: Project) -> None:
    """The draft veto is what put the reading at the finish line.

    A measured 18-page run spent its first eleven builds in draft, so a reading held
    back for the delivered build read all 18 pages at once, reported 45 problems, and
    was answered by one edit and a publish 38 seconds later -- there is no acting on 45
    findings when acting means redoing every page. Five unread pages is where the same
    findings still change the pages that come after them.
    """
    quiet = _Reader()
    early = _tool(project, _ok(project, pages=4))
    early.review = quiet

    await early.execute(project=project.slug, draft=True)
    assert quiet.calls == [], "four unread pages does not fire it"

    reader = _Reader()
    tool = _tool(project, _ok(project, pages=5))
    tool.review = reader

    reply = await tool.execute(project=project.slug, draft=True)
    said = reply if isinstance(reply, str) else reply.model_text

    assert reader.calls == [project.slug], "and the fifth does, on a draft"
    assert reader.asked == [[1, 2, 3, 4, 5]]
    assert "first_reading" in said


async def test_nothing_is_read_while_something_refuses_the_build(project: Project) -> None:
    """A page failing a gate is about to change, and a refused build is not a deck.

    The refusal by kind rather than by severity is the case worth driving: it leaves
    the stage's own result `ok`, so only the tool's blocking set stands between the
    reading and a page that is on its way to being rewritten anyway.
    """
    reader = _Reader()
    gated = _ok(
        project,
        findings=(Finding(kind="unplaced_figure", severity=Severity.WARNING, message="page 1 planned Fig. 2", page=1),),
        pages=6,
    )
    tool = _tool(project, gated)
    tool.review = reader

    await tool.execute(project=project.slug, draft=True)
    assert reader.calls == [], "a page a gate refuses is about to change"

    refused = replace(_ok(project, pages=6), ok=False, note="the built deck is empty")
    refused = replace(refused, data={k: v for k, v in refused.data.items() if k != "pptx_path"})
    undelivered = _tool(project, refused)
    undelivered.review = reader
    await undelivered.execute(project=project.slug)
    assert reader.calls == [], "nothing was published, so there is nothing to read"


async def test_a_page_rewritten_after_its_reading_is_read_again(project: Project) -> None:
    """The load-bearing half of reading a deck early: the mark is per page-version.

    Marked per page number, an early reading of a page the author later rewrites would
    ship the rewritten version with nobody having looked at it -- which is worse than
    the finish-line reading it replaces. The version is the build record's fingerprint
    of the code that drew the page, so the pages left alone stay read.
    """
    reader = _Reader(deck=project)
    tool = _tool(project, _ok(project, pages=6, sources=_versioned(project, 6)))
    tool.review = reader

    await tool.execute(project=project.slug, draft=True)
    assert reader.asked == [[1, 2, 3, 4, 5, 6]], "the whole deck was unread"

    await tool.execute(project=project.slug, draft=True)
    assert reader.calls == [project.slug], "nothing was rewritten, so there is nothing to read again"

    _versioned(project, 6, rewritten=4)
    await tool.execute(project=project.slug)

    assert reader.asked[-1] == [4], "the page whose code changed, and only that page"


async def test_a_delivered_build_leaves_no_page_version_unread(project: Project) -> None:
    """The invariant the early reading is not allowed to cost.

    A delivered build reads whatever is outstanding rather than waiting for five of
    them, so the deck that ships has had every one of its page-versions read by
    somebody who did not write it.
    """
    reader = _Reader(deck=project)
    drafting = _tool(project, _ok(project, pages=5, sources=_versioned(project, 5)))
    drafting.review = reader

    await drafting.execute(project=project.slug, draft=True)
    assert reader.asked == [[1, 2, 3, 4, 5]]

    whole = _tool(project, _ok(project, pages=8, sources=_versioned(project, 8)))
    whole.review = reader
    await whole.execute(project=project.slug)

    assert reader.asked[-1] == [6, 7, 8], "three unread pages is under five, and a delivered build reads them"
    assert set(range(1, 9)) - _already_read(project) == set(), "so nothing ships unread"


async def test_the_deck_is_read_once_and_not_on_every_build(project: Project) -> None:
    """Every build after the first would pay for a whole deck of model calls to say
    what the author has already been told. The record on disk is what remembers."""
    reader = _Reader(deck=project)
    tool = _tool(project, _ok(project))
    tool.review = reader

    await tool.execute(project=project.slug)
    await tool.execute(project=project.slug)
    await tool.execute(project=project.slug)

    assert reader.calls == [project.slug], "read once, on the first finished deck"


async def test_a_deck_longer_than_one_reading_is_read_across_builds(project: Project) -> None:
    """`ppt_outline` takes up to 40 pages and one reading covers 30.

    The hook used to skip on "a record exists", so a 40-page deck was marked read
    after 30 and pages 31-40 never got the reading the tool description promises.
    Coverage is the test now, so the next delivered build reads the remainder.
    """
    reader = _Reader(deck=project, covers=range(1, 31))
    tool = _tool(project, _ok(project, pages=40))
    tool.review = reader

    await tool.execute(project=project.slug)
    assert reader.calls == [project.slug], "thirty of forty pages is not a deck that was read"

    # The second round covers the rest. Its record has to hold the union: a record of
    # only the round that just ran is {31-40} plus whatever filled the cap, and the
    # deck alternates between two sets forever without ever being covered.
    reader.covers = range(31, 41)
    await tool.execute(project=project.slug)
    await tool.execute(project=project.slug)

    assert reader.calls == [project.slug] * 2, "the deck was covered and read a third time"
    read = json.loads((project.review_dir / RECORD_FILE).read_text())["pages_read"]
    assert sorted(int(page) for page in read) == list(range(1, 41)), read


async def test_a_reading_that_read_nothing_leaves_no_record(project: Project) -> None:
    """A transient total failure used to disable the reading for the deck's whole life.

    Every reply empty or unparsable meant no page was read, the reply still wrote
    `review.json`, and every later build skipped the hook because that file was there.
    Reported on the merge request and reproduced with `_asks({}, [], [1, 2])`.
    """
    reader = _Reader(deck=project, covers=())
    tool = _tool(project, _ok(project))
    tool.review = reader

    await tool.execute(project=project.slug)
    await tool.execute(project=project.slug)

    assert reader.calls == [project.slug] * 2, "a reading that covered no page is not a reading"


async def test_a_reading_that_fails_does_not_cost_the_delivery(project: Project) -> None:
    """The deck is built, gated and published before this runs. A reviewer that threw,
    answered nothing, or answered something unparseable must leave all of that standing.
    """

    class Broken(_Reader):
        async def execute(self, project: str, **_kwargs):
            self.calls.append(project)
            raise RuntimeError("the gateway said 503")

    reader = Broken()
    tool = _tool(project, _ok(project))
    tool.review = reader

    reply = await tool.execute(project=project.slug)
    body = _body(reply)

    assert reader.calls == [project.slug]
    assert "first_reading" not in body, "a reading that failed contributes nothing"
    assert body["pptx_path"], "and the deck is still delivered"


async def test_without_a_reviewer_the_build_is_what_it_always_was(project: Project) -> None:
    """A build with no provider registers no reviewer, and that route must be the
    plain one rather than a broken one."""
    tool = _tool(project, _ok(project))
    tool.review = None

    body = _body(await tool.execute(project=project.slug))

    assert "first_reading" not in body
    assert body["pptx_path"]


@pytest.mark.asyncio
async def test_what_the_script_warned_about_reaches_the_author_on_a_successful_build(project: Project) -> None:
    """A picture cropped past what its frame can hold used to crash the build; now it
    warns. A warning nobody reads is a crash with worse manners, so the script's stderr
    comes back beside the findings when the build succeeded, not only when it failed."""
    deck = project.build_dir / "deck.pptx"
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"PK")
    result = StageResult(
        ok=True,
        data={
            "outcome": BuildOutcome(
                ok=True,
                pptx_path=deck,
                pages=1,
                stderr="build.py:12: UserWarning: banner.jpg is 1400x932 (1.50) and this frame is 13.33x3.23in -- 2.8x apart.\n",
            )
        },
    )

    body = _body(await _tool(project, result).execute(project="tarvis"))

    assert "2.8x apart" in body["warnings"]
    assert body["warnings"] == body["warnings"].strip()


@pytest.mark.asyncio
async def test_a_reply_with_findings_asks_for_the_edits_and_the_build_together(project: Project) -> None:
    """The shape of the next reply, said where the author reads it: a run spent 32 of
    72 iterations sending one edit and one build as separate replies."""
    views = FakeViews(pages=2)
    body = _body(
        await _tool(
            project,
            _ok(project, findings=[Finding(kind="band", severity=Severity.WARNING, message="a filled bar", page=1)]),
            views,
        ).execute(project="tarvis")
    )
    assert "in one reply" in body["next_step"]

    clean = _body(await _tool(project, _ok(project), FakeViews(pages=2)).execute(project="tarvis"))
    assert "in one reply" not in clean["next_step"], "a delivered deck with nothing found has no next edit"


async def test_a_republished_decks_small_revision_does_not_rearm_the_reader(project: Project) -> None:
    """Three republishes of one live deck each re-read the one to three pages just edited,
    240s apiece, for nine page opinions in twelve minutes. A revision of a deck already
    delivered reads only when as many pages are unread as a draft asks for; the first
    delivery still reads whatever is left."""
    reader = _Reader(deck=project)
    tool = _tool(project, _ok(project, pages=3, sources=_versioned(project, 3), republished=True))
    tool.review = reader
    await tool.execute(project=project.slug)
    assert reader.asked == [], "three unread pages on a republish are under the floor"

    fresh = _Reader(deck=project)
    first = _tool(project, _ok(project, pages=3, sources=_versioned(project, 3)))
    first.review = fresh
    await first.execute(project=project.slug)
    assert fresh.asked == [[1, 2, 3]], "a first delivery reads what is left"


async def test_what_the_reader_said_and_nobody_answered_rides_on_every_build_reply(project: Project) -> None:
    """A reading lives in one reply. A live run delivered a deck with a paragraph the
    reader had reported buried three builds earlier; the ledger is the sentence that
    says "page 14 is still open" on every build until it is answered.
    """
    from raven_ppt.services import review_ledger

    review_ledger.record_reading(
        project,
        {2: [{"kind": "figure", "where": "the right column", "what": "an illustration over the copy", "fix": ""}]},
        {},
        1,
    )
    tool = _tool(project, _ok(project))
    tool.review = _Reader()

    body = _body(await tool.execute(project=project.slug))

    assert body["open_findings"]["count"] == 1
    assert body["open_findings"]["pages"]["2"][0]["kind"] == "figure"
    assert "still open on page(s) 2" in body["next_step"]
    assert "dismiss" in body["next_step"]


async def test_the_pages_a_build_names_are_read_before_the_backlog(project: Project) -> None:
    """A build of `slides=[4]` on a live deck read the backlog and left page 4 -- the one
    the author was waiting on -- unread when the budget ran out.
    """
    reader = _Reader()
    tool = _tool(project, _ok(project))
    tool.review = reader

    await tool.execute(project=project.slug, slides=[2])

    assert reader.asked == [[2, 1]]


@pytest.mark.asyncio
async def test_the_tiers_build_cap_counts_a_draft_and_releases_one(project: Project) -> None:
    """A draft counts as a whole build and the cap releases a draft. Counting only
    finished builds made the cap inert on the runs that needed it: one_door made 13
    draft builds and never wrote a builds.json at all, a medium-tier run made 7 and the
    same, and across four runs the author passed `draft: true` on 7/7, 13/13 and 5/5 of
    its calls -- so a cap that only counts `draft: false` is a cap on a road the author
    does not take. A draft already costs a whole build: `measure` renders before the
    stage's draft branch, and `_DRAFT_EXEMPT` waives three finding kinds.

    Every reply says the count, because a budget the author cannot see is one it spends
    as though it were endless."""
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 3, "readingCap": 3}, "high")
    tool = _tool(project, _ok(project))
    stage = tool.stage

    first = _body(await tool.execute(project="tarvis", draft=True))
    assert tier.whole_builds_taken(project) == 1, "a draft counts"
    assert "whole-deck build 1 of the high tier's 3" in first["build_budget"]
    assert "a draft counts as one" in first["build_budget"]

    await tool.execute(project="tarvis", draft=True)
    assert stage.releases == [False, False]
    assert tier.whole_builds_taken(project) == 2

    last = _body(await tool.execute(project="tarvis", draft=True))
    assert stage.releases[-1] is True, "the draft that reaches the cap releases the deck"
    assert tier.whole_builds_taken(project) == 3
    assert "whole-deck build 3 of the high tier's 3" in last["build_budget"]

    tier.write_mode(project.workspace, {}, "max")
    uncapped = _body(await tool.execute(project="tarvis", draft=True))
    assert stage.releases[-1] is False, "the max tier caps nothing"
    assert uncapped["build_budget"] == "whole-deck build 4; this max tier sets no cap"


@pytest.mark.asyncio
async def test_a_deck_released_at_the_cap_is_returned_as_delivered(project: Project) -> None:
    """The stage keeps the findings it released past in the result, as reports. The
    reply must not derive a refusal from them again: a deck returned with a pptx_path,
    `ok: false` and "not published" in one breath kept an author building past the
    cap it had reached, and skipped the final reading a delivery gets."""
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 1, "readingCap": 3}, "high")
    held = Finding(kind="citation", severity=Severity.BLOCKING, message="page 1 cites Fig. 4, shows Fig. 5", page=1)
    released = replace(_ok(project, findings=(held,), released=["citation"]), ok=True)
    reader = _Reader()
    tool = _tool(project, released)
    tool.review = reader

    body = _body(await tool.execute(project="tarvis"))

    assert tool.stage.releases == [True]
    assert body["ok"] is True and "error" not in body and "not_delivered" not in body
    assert body["pptx_path"].endswith("deck.pptx")
    assert body["released_at_cap"].startswith("whole-deck build 1 of the high tier's 1")
    assert "citation" in str(body["measured"]), "the released findings stay in the reply as reports"
    assert "this deck is delivered at " in body["next_step"]
    assert "nothing was published" not in body["next_step"]
    assert reader.asked, "a delivered deck gets its final reading whether or not the cap released it"


@pytest.mark.asyncio
async def test_a_build_that_lost_a_page_to_a_crash_is_not_charged_the_cap(project: Project) -> None:
    """The same argument as the script that produced no deck, applied to a page.

    A live medium run spent its tenth and last build on two pages lost to one undefined
    name: the deck published because the cap had been reached, the reply said nothing
    refused it, and the author never saw either the crash or the five copy findings on
    the pages that did draw. A build that lost a page is not a whole build of the deck,
    so it goes against its own allowance -- and the reply says which, either way.
    """
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 3, "readingCap": 3}, "medium")
    raised = Finding(kind="page_failed", severity=Severity.BLOCKING, message="page 2's block raised", page=2)
    tool = _tool(project, replace(_ok(project, findings=(raised,), released=["page_failed"]), ok=True))

    first = _body(await tool.execute(project="tarvis"))
    assert tier.whole_builds_taken(project) == 0, "the build that lost page 2 is not one of the three"
    assert tier.reprieves_taken(project) == 1
    assert "reprieve 1 of 2" in first["lost_pages_not_counted"]
    assert "page(s) 2 did not draw" in first["lost_pages_not_counted"]

    _body(await tool.execute(project="tarvis"))
    assert (tier.whole_builds_taken(project), tier.reprieves_taken(project)) == (0, 2)

    # And the third is charged, which is what bounds the total at the cap plus the
    # allowance: a deck cannot buy builds by crashing.
    third = _body(await tool.execute(project="tarvis"))
    assert tier.whole_builds_taken(project) == 1
    assert "all 2 of its reprieve(s)" in third["lost_pages_counted"]
    assert "lost_pages_not_counted" not in third


@pytest.mark.asyncio
async def test_a_published_build_that_lost_pages_does_not_read_as_finished(project: Project) -> None:
    """What the cap's release turned into, and the whole of defect two.

    `stages/build.py` empties the blocking list when it releases at the cap -- rightly,
    because the user gets a deck rather than nothing -- and the reply then told the
    author the deck was delivered and nothing refused it. That is the wrong answer to a
    stand-in page: publication is not the question a `NameError` asks, and the next step
    is one edit and one more build.
    """
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 1, "readingCap": 3}, "medium")
    raised = Finding(kind="page_failed", severity=Severity.BLOCKING, message="page 10's block raised", page=10)
    released = replace(_ok(project, findings=(raised,), released=["page_failed"]), ok=True)
    tool = _tool(project, released)
    tool.review = _Reader()

    body = _body(await tool.execute(project="tarvis"))

    assert body["ok"] is True and body["pptx_path"].endswith("deck.pptx"), "the deck still goes out"
    assert "page(s) 10 are not in it" in body["next_step"]
    assert "nothing refuses it" not in body["next_step"]
    assert "not counted against this tier's builds" in body["next_step"]


@pytest.mark.asyncio
async def test_the_last_permitted_build_losing_a_page_still_delivers(project: Project) -> None:
    """What happens when the reprieves are gone and the build crashes anyway.

    It publishes, and it says so: the deck the user keeps is the one with the stand-in
    page in it, and the reply says that in as many words rather than asking for a build
    the deck no longer has. That is the terminating end of the allowance -- the run
    always ends with a deck, never with nothing and never with an unbounded budget.
    """
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 1, "readingCap": 3}, "medium")
    raised = Finding(kind="page_failed", severity=Severity.BLOCKING, message="page 10's block raised", page=10)
    released = replace(_ok(project, findings=(raised,), released=["page_failed"]), ok=True)
    tool = _tool(project, released)
    tool.review = _Reader()
    for _ in range(tier.CRASH_REPRIEVES):
        await tool.execute(project="tarvis")

    body = _body(await tool.execute(project="tarvis"))

    assert body["ok"] is True and body["pptx_path"].endswith("deck.pptx")
    assert "reprieves for a lost page are spent" in body["next_step"]
    assert "what is delivered now is what the user keeps" in body["next_step"]


@pytest.mark.asyncio
async def test_an_edited_delivery_is_named_first_in_the_reply(project: Project) -> None:
    """The stage's reading has to reach the author, and above the page findings.

    It is not about a page: the deck the user was holding had a change in it that no
    gate and no reader ever saw, and the answer is to put that change in the program.
    """
    changed = "out/deck.pptx was not the deck this route delivered: its bytes changed after it was published"
    tool = _tool(project, _ok(project, delivery_changed=changed))
    tool.review = _Reader()

    body = _body(await tool.execute(project="tarvis"))

    assert body["delivery_changed"] == changed
    # First of the instructions, ahead of the page findings and the delivery line.
    step = body["next_step"]
    assert changed in step
    assert step.index(changed) < step.index("this deck is delivered at")


async def test_a_deck_the_cap_refused_is_not_reported_as_delivered(project: Project) -> None:
    """`released` says the stage stopped holding the findings back, not that the file
    went out: publication can still refuse for a reason of its own -- a staged deck that
    vanished, a deck that changed after it was measured -- and the stage returns those
    with `released` already in the data. Clearing the refusal on `released` alone
    answered that with a delivered deck and a cap line, and nothing then said the deck
    had not been written."""
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 1, "readingCap": 3}, "high")
    held = Finding(kind="citation", severity=Severity.BLOCKING, message="page 1 cites Fig. 4", page=1)
    built = _ok(project, findings=(held,), released=["citation"])
    refused = replace(
        built,
        ok=False,
        data={key: value for key, value in built.data.items() if key != "pptx_path"},
        note="the deck changed after it was checked; build it again so the checks describe it",
    )

    body = _body(await _tool(project, refused).execute(project="tarvis"))

    assert body["ok"] is False
    assert "released_at_cap" not in body, "a refused deck was reported as delivered as it stands"
    assert "the deck changed after it was checked" in body["not_delivered"]


@pytest.mark.asyncio
async def test_the_reply_names_the_pages_the_delivery_left_out(project: Project) -> None:
    """The cap delivers the pages that drew and leaves the runner's stand-ins out, so
    the file is shorter than the deck every finding above it is numbered against. Unsaid,
    that reads as a deck that was always this long -- and as page numbers that point at
    the wrong pages once the reader opens it."""
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 1, "readingCap": 3}, "high")
    stood_in = Finding(kind="page_failed", severity=Severity.BLOCKING, message="page 2's block raised", page=2)
    delivered = _ok(project, findings=(stood_in,), pages=3, released=["page_failed"], dropped_pages=[2])
    body = _body(await _tool(project, replace(delivered, ok=True)).execute(project="tarvis"))

    assert body["ok"] is True
    assert "page(s) 2 did not draw" in body["pages_left_out"]
    assert "2 of 3 pages" in body["pages_left_out"]
    assert "numbered one lower" in body["pages_left_out"]


@pytest.mark.asyncio
async def test_a_script_that_produced_no_deck_is_not_a_whole_build(project: Project) -> None:
    """The count that reaches the cap is of decks built, not of scripts run: two syntax
    errors under a cap of two must not release the first deck that exists past its
    blocking findings."""
    from raven_ppt.services import tier

    tier.write_mode(project.workspace, {"buildCap": 2, "readingCap": 3}, "high")
    failed = StageResult(ok=False, data={"outcome": BuildOutcome(ok=False, stderr="SyntaxError: nope")})
    tool = _tool(project, failed)

    await tool.execute(project="tarvis")
    await tool.execute(project="tarvis")
    assert tier.whole_builds_taken(project) == 0
    assert tool.stage.releases == [False, False]

    tool.stage.result = _ok(project)
    await tool.execute(project="tarvis")
    assert tier.whole_builds_taken(project) == 1
    assert tool.stage.releases[-1] is False, "the first deck that exists is the first whole build"


# -- the destination the user named ------------------------------------------


async def test_a_destination_the_publish_step_cannot_promise_is_refused_before_the_build(project: Project) -> None:
    from raven_ppt.services.publish import read_destination

    tool = _tool(project, _ok(project))
    body = _body(await tool.execute(project="tarvis", deliver_to="handoff/intro.pptx"))

    assert body["ok"] is False and "not an absolute path" in body["error"]
    assert "deliver_to" in body["hint"]
    assert read_destination(project) is None
    assert tool.stage.calls == [], "nothing was built"


async def test_a_destination_stated_on_a_refused_build_is_kept_all_the_same(tmp_path: Path) -> None:
    """Stated on the first call and refused for a missing outline: the author fixes the
    outline and builds again without repeating it, and the deck still lands there."""
    from raven_ppt.services.publish import read_destination

    bare = Project(workspace=tmp_path, slug="unplanned")
    write_brief(DeckBrief(language="English", audience="a review", pages=PageBudget(1, 40)), brief_path(bare))
    write_plan(IntakePlan(topic="a deck", digest="x"), intake_path(bare))
    wanted = tmp_path / "handoff" / "intro.pptx"

    body = _body(await _tool(bare, _ok(bare)).execute(project="unplanned", deliver_to=str(wanted)))

    assert body["ok"] is False and "no outline recorded" in body["error"]
    assert body["deliver_to"] == str(wanted)
    assert read_destination(bare) == wanted


async def test_a_directory_destination_takes_the_stages_own_deck_name(project: Project, tmp_path: Path) -> None:
    from raven_ppt.services.publish import read_destination

    handoff = tmp_path / "handoff"
    body = _body(await _tool(project, _ok(project)).execute(project="tarvis", draft=True, deliver_to=f"{handoff}/"))

    assert body["ok"] is True
    assert body["deliver_to"] == str(handoff / "deck.pptx") == str(read_destination(project))
    assert f"the destination {handoff / 'deck.pptx'} is kept for this deck" in body["next_step"]


async def test_the_delivered_path_and_the_slide_count_are_words_the_author_repeats(
    project: Project, tmp_path: Path
) -> None:
    delivered = tmp_path / "handoff" / "ravenx-intro.pptx"
    result = _ok(project, pages=12, delivered_to=str(delivered), delivered_pdf=str(delivered.with_suffix(".pdf")))

    body = _body(await _tool(project, result).execute(project="tarvis"))

    assert body["ok"] is True and body["delivered_to"] == str(delivered)
    assert body["next_step"].startswith(f"tell the user in these terms: the deck is at {delivered} and has 12 slides")
    assert f"its PDF preview is {delivered.with_suffix('.pdf')}" in body["next_step"]
    assert body["pptx_path"] in body["next_step"], "out/ is still named as the engine's own copy"


async def test_a_destination_that_could_not_be_written_is_the_first_thing_said(project: Project) -> None:
    result = _ok(project, delivery_failed="/handoff/intro.pptx: could not write /handoff/intro.pptx: Permission denied")

    body = _body(await _tool(project, result).execute(project="tarvis"))

    assert body["ok"] is True
    assert body["next_step"].startswith("the deck is published under out/ but not at the destination the user named")
    assert "Permission denied" in body["next_step"]


async def test_a_refused_build_says_the_destination_still_holds_the_last_deck(project: Project, tmp_path: Path) -> None:
    from raven_ppt.services.publish.deliver import record_published

    delivered = tmp_path / "handoff" / "ravenx-intro.pptx"
    record_published(project, delivered, "abc", 12, role="delivery")
    refused = replace(_ok(project), ok=False, note="the built deck is empty")
    refused = replace(refused, data={k: v for k, v in refused.data.items() if k != "pptx_path"})

    body = _body(await _tool(project, refused).execute(project="tarvis"))

    assert body["destination_unchanged"].startswith(f"{delivered} still holds the last published deck")
    assert "delivered" not in body["next_step"]


@pytest.mark.asyncio
async def test_every_draft_build_says_that_a_draft_does_not_deliver(project: Project) -> None:
    """Four measured runs' builds: stack5 took 17 finished builds and published; stack6
    took 5 drafts and 0 finished, one_door 13 and 0, medium 1 finished -- and none of
    those three published. Every run that never left draft never delivered, and `draft`
    is the author's own switch, so the party choosing whether to be judged is the party
    the gates exist for.

    The reply used to carry this sentence in the next step below, behind
    `"pptx_path" in payload` -- which a draft never has, because the stage returns before
    publication. It was unreachable on the one path that needed it. Now it is its own
    line on every draft, and absent from a finished build, which has delivered."""
    built = _ok(project, pages=4)
    # A draft as the stage really returns one: `draft` set and no `pptx_path`, because it
    # returns before publication. The older pin used a stage that hands back a path on a
    # draft too, which is why the next step below read as reachable when it was not.
    drafted = replace(
        built, data={key: value for key, value in built.data.items() if key != "pptx_path"} | {"draft": True}
    )
    body = _body(await _tool(project, drafted).execute(project="tarvis", draft=True))

    assert "a draft is not what delivers them" in body["draft_does_not_deliver"]
    assert "`draft: false`" in body["draft_does_not_deliver"]
    assert "build again without `draft`" in str(body["next_step"]), "the draft's next step is reachable too"

    finished = _body(await _tool(project, _ok(project, pages=4)).execute(project="tarvis"))
    assert "draft_does_not_deliver" not in finished, "a finished build has delivered; it needs no such line"


@pytest.mark.asyncio
async def test_a_draft_asked_for_a_page_it_has_not_drawn_yet_does_not_claim_it(project: Project) -> None:
    """The sentence opens on the pages being drawn, so it is only true once the pages
    the call named exist. A draft asked for page 7 of a four-page program has not drawn
    what it was asked for, and the reply says so elsewhere."""
    drafted = replace(_ok(project, pages=4), data={**_ok(project, pages=4).data, "draft": True})
    body = _body(await _tool(project, drafted).execute(project="tarvis", draft=True, slides=[7]))

    assert "draft_does_not_deliver" not in body
