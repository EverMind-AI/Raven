"""Reading the sentence a deck task arrived as, and getting the project ready.

The tests that matter here are about the line between what the request *says* and
what a model would *guess*, because that line decides whether the deck is later
measured against a decision the user made or one nobody did.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pptx")

from raven_ppt.contracts import (  # noqa: E402
    DeckBrief,
    IntakePlan,
    Outline,
    PageBudget,
    PagePlan,
    Project,
    brief_path,
    intake_path,
    load_brief,
    load_plan,
    outline_path,
    write_brief,
    write_outline,
    write_plan,
)
from raven_ppt.services import state as deck_state  # noqa: E402
from raven_ppt.services.ingest import ingest_materials  # noqa: E402
from raven_ppt.stages.prepare import BRIEF_QUESTIONS, PrepareStage  # noqa: E402
from raven_ppt.tools.prepare import PptPrepareTool  # noqa: E402
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401


class FakeComposer:
    """Answers scripted per call, and records the briefs and parts it was given."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[str, list[dict[str, Any]]]] = []

    async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
        self.seen.append((system, parts))
        return self.replies.pop(0) if self.replies else json.dumps({"topic": "a deck"})

    def texts(self) -> str:
        return "\n".join(part.get("text", "") for _system, parts in self.seen for part in parts)


def _plan(**fields: Any) -> str:
    return json.dumps({"topic": "video segmentation", **fields})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def materials(workspace: Path, image) -> Path:
    """A conventional materials directory: one document and one figure."""
    where = workspace / "materials"
    where.mkdir()
    (where / "notes.md").write_text(
        "# TarViS\n\nThe model reaches 44 FPS on a single A100 and was trained on 51k videos.\n",
        encoding="utf-8",
    )
    (where / "diagram.png").write_bytes(image("diagram.png", (30, 90, 160)).read_bytes())
    return where


@pytest.fixture
def project(workspace: Path) -> Project:
    return Project(workspace=workspace, slug="talk")


def _stage(*replies: str) -> tuple[PrepareStage, FakeComposer]:
    composer = FakeComposer(*replies)
    return PrepareStage(composer=composer, ingest=ingest_materials), composer


# --- what is counted ------------------------------------------------------


def test_an_empty_project_counts_nothing_and_does_not_raise(project: Project):
    state = deck_state.read(project)

    assert state.brief is None
    assert state.template is None
    assert not state.sources and not state.figures
    assert not state.ingested
    assert "Sources ingested: none" in state.summary()


def test_what_was_ingested_is_counted_with_the_label_its_source_gave_it(project: Project, materials: Path):
    ingest_materials(materials, project.ingest_dir)

    state = deck_state.read(project)

    assert state.ingested
    assert state.sources
    assert state.figures
    assert state.materials_chars > 0
    assert "44 FPS" in state.excerpt


def test_a_pptx_nobody_bound_is_counted_until_it_is(project: Project, template_file):
    """A user who drops their house style into the workspace and says "use this"
    has done everything they can; a run that never binds it builds a white deck
    beside their template without either party noticing."""
    from raven_ppt.services.template import bind

    template_file(where=project.workspace / "uploads")

    assert deck_state.read(project).unbound_templates == ("uploads/house-style.pptx",)

    bind(project.workspace / "uploads" / "house-style.pptx", project)

    assert deck_state.read(project).template is not None


def test_the_digest_moves_with_the_inputs_and_not_with_the_deck(project: Project, materials: Path):
    """What a reading of the task depends on, and nothing else: a document arriving
    invalidates it, twenty rebuilds do not."""
    before = deck_state.read(project).digest()

    project.build_dir.mkdir(parents=True, exist_ok=True)
    (project.build_dir / "build.py").write_text("# a program", encoding="utf-8")
    assert deck_state.read(project).digest() == before

    ingest_materials(materials, project.ingest_dir)
    assert deck_state.read(project).digest() != before


# --- reading the task -----------------------------------------------------


async def test_the_materials_are_read_before_the_model_is_asked(project: Project, materials: Path):
    """The call's real question is what this deck still needs, and a call that has not
    seen the figures already extracted asks for images the deck already has."""
    stage, composer = _stage(_plan())

    result = await stage.run(project, "make a deck about this")

    assert result.ok
    assert any("read" in line for line in result.data["done"])
    said = composer.texts()
    assert "Figures extracted (1)" in said
    assert "44 FPS" in said


async def test_a_request_that_states_the_brief_is_not_asked_back(project: Project, materials: Path):
    """The point of reading the request at all. "给投资人做一份 15 页中文路演" answers
    all three, and asking them again is asking a user to repeat themselves."""
    stage, _ = _stage(_plan(stated={"language": "中文", "audience": "投资人路演", "pages_low": 15, "pages_high": 15}))

    result = await stage.run(project, "给投资人做一份 15 页中文路演")

    brief = load_brief(brief_path(project))
    assert brief is not None
    assert brief.language == "中文"
    assert brief.pages.low == 15 and brief.pages.high == 15
    assert result.data["plan"].questions == ()


async def test_a_request_that_rules_something_out_binds_it_to_the_deck(project: Project, materials: Path):
    """ "这份不要用 icon" is a decision, and before the brief had a slot for it the
    only place it could go was prose thirty turns upstream of the build that would
    have to honour it."""
    stage, _ = _stage(
        _plan(
            stated={
                "language": "中文",
                "audience": "内部技术评审",
                "pages_low": 12,
                "pages_high": 12,
                "forbidden": ["不要用 icon", "  "],
            }
        )
    )

    await stage.run(project, "做一份 12 页中文内部评审材料，这份不要用 icon")

    brief = load_brief(brief_path(project))
    assert brief is not None
    assert brief.forbidden == ("不要用 icon",), "and the blank entry never became a rule"


async def test_one_prohibition_written_as_a_string_is_still_read(project: Project, materials: Path):
    """A reply that answers `"forbidden": "no icons"` instead of a list means the
    user forbade icons; dropping it is the one wrong reading available."""
    stage, _ = _stage(_plan(stated={"forbidden": "no icons"}))

    result = await stage.run(project, "make a deck, no icons")

    assert result.data["plan"].stated.forbidden == ("no icons",)
    assert "forbidden" not in result.data["plan"].stated.missing, "nobody is asked what they did not forbid"


async def test_a_prohibition_that_came_back_as_an_object_is_dropped_not_stringified(project: Project, materials: Path):
    """`str()` on a dict does not recover what the model meant by it.

    A reply holding `"forbidden": [{"what": "no icons"}]` came through as the literal
    text `{'what': 'no icons'}`, went into the brief as a rule, and was quoted back on
    every build verbatim -- a line of Python presented to the author as something the
    user said. There is no reading of it that is better than none, so it goes the way
    the blank entries go.
    """
    stage, _ = _stage(
        _plan(
            stated={
                "language": "中文",
                "audience": "内部技术评审",
                "pages_low": 12,
                "pages_high": 12,
                "forbidden": [{"what": "no icons"}, "不要用 icon", 7, None],
            }
        )
    )

    result = await stage.run(project, "做一份 12 页中文内部评审材料，这份不要用 icon")

    assert result.data["plan"].stated.forbidden == ("不要用 icon",)
    brief = load_brief(brief_path(project))
    assert brief is not None and brief.forbidden == ("不要用 icon",)


async def test_half_a_brief_is_not_recorded_at_all(project: Project, materials: Path):
    """A brief exists to be checked against, so half of one filled out with
    defaults would have the deck measured against a decision nobody made -- worse
    than the refusal that stands while it is missing, because it looks agreed."""
    stage, _ = _stage(_plan(stated={"language": "English", "audience": None, "pages_low": None}))

    result = await stage.run(project, "make a deck in English")

    assert load_brief(brief_path(project)) is None
    asked = " ".join(question.question for question in result.data["plan"].questions)
    assert "Who is this for" in asked
    assert "How many slides" in asked
    assert "What language" not in asked, "the request stated the language"


async def test_the_brief_questions_are_added_by_code_not_left_to_the_model(project: Project, materials: Path):
    """They gate the build, so a reading that forgot one would strand the deck
    behind a refusal with nothing telling anyone what to ask."""
    stage, _ = _stage(_plan())

    result = await stage.run(project, "make me a deck")

    assert set(BRIEF_QUESTIONS) == {"language", "audience", "pages"}
    asked = {question.question for question in result.data["plan"].questions}
    assert asked == {question.question for question in BRIEF_QUESTIONS.values()}


async def test_a_recorded_brief_stops_the_questions(project: Project, materials: Path):
    write_brief(
        DeckBrief(language="English", audience="an internal review", pages=PageBudget(10, 12)),
        brief_path(project),
    )
    stage, _ = _stage(_plan())

    result = await stage.run(project, "make me a deck")

    assert result.data["plan"].questions == ()


async def test_a_directory_the_request_names_is_ingested(project: Project, workspace: Path, image):
    """ "材料在 ./papers" is the commonest thing a one-line task carries."""
    papers = workspace / "papers"
    papers.mkdir()
    (papers / "paper.md").write_text("# A paper\n\nIt reports 91.2 mIoU.\n", encoding="utf-8")
    stage, _ = _stage(_plan(materials_dir="papers"))

    result = await stage.run(project, "make a deck, materials are in ./papers")

    assert "91.2 mIoU" in deck_state.read(project).excerpt
    assert any("papers" in line for line in result.data["done"])


async def test_a_template_the_request_names_is_bound(project: Project, workspace: Path, template_file):
    template_file(name="house.pptx", where=workspace)
    stage, _ = _stage(_plan(template="house.pptx"))

    result = await stage.run(project, "use house.pptx")

    assert deck_state.read(project).template is not None
    assert any("bound house.pptx" in line for line in result.data["done"])


async def test_a_path_outside_the_workspace_binds_nothing(project: Project, tmp_path: Path, template_file):
    outside = template_file(name="elsewhere.pptx", where=tmp_path.parent)
    stage, _ = _stage(_plan(template=f"../{outside.name}"))

    result = await stage.run(project, "use that one")

    assert deck_state.read(project).template is None
    assert any("not a file in the workspace" in line for line in result.data["done"])


async def test_the_errands_and_the_notes_survive(project: Project, materials: Path):
    stage, _ = _stage(
        _plan(
            errands=[{"what": "an architecture diagram", "why": "page 4 has nothing to show", "how": "web_search"}],
            notes=["开头要有一页讲动机"],
        )
    )

    result = await stage.run(project, "make a deck")

    plan = result.data["plan"]
    assert plan.errands[0].what == "an architecture diagram"
    assert plan.notes == ("开头要有一页讲动机",)
    assert plan.outstanding


def _text_only(workspace: Path) -> Path:
    """Materials with nothing to extract and a URL that has something."""
    where = workspace / "text-only"
    where.mkdir()
    (where / "analysis.md").write_text(
        "# Competitors\n\nMem0 documents its pipeline at https://docs.mem0.ai/core-concepts/how-it-works.\n",
        encoding="utf-8",
    )
    return where


async def test_an_errand_naming_a_picture_no_longer_deletes_the_sweep(project: Project, workspace: Path):
    """The loophole: "image" is a substring of `web_search(kind="images")`, so an author
    that mentioned a picture search for one page deleted the errand asking it to sweep
    the sources -- which is the one thing said about that sweep before `ppt_outline`
    refuses over it."""
    _text_only(workspace)
    stage, _ = _stage(
        _plan(
            materials_dir="text-only",
            errands=[{"what": "a product shot", "why": "page 4 shows nothing", "how": 'web_search(kind="images")'}],
        )
    )

    result = await stage.run(project, "make a deck")

    errands = result.data["plan"].errands
    swept = [errand for errand in errands if errand.what == "the figures the sources themselves point at"]
    assert len(swept) == 1
    assert "cite 1 URL(s)" in swept[0].why
    assert "`swept`" in swept[0].how


async def test_the_sweep_errand_is_not_added_once_there_are_figures(project: Project, materials: Path):
    """Any number above zero is a judgement about these figures and these pages, and
    that one is the author's."""
    stage, _ = _stage(_plan())

    result = await stage.run(project, "make a deck")

    assert deck_state.read(project).figures
    assert all(errand.what != "the figures the sources themselves point at" for errand in result.data["plan"].errands)


# --- not doing the work twice --------------------------------------------


async def test_an_unchanged_project_is_not_read_again(project: Project, materials: Path):
    stage, composer = _stage(_plan(), _plan())
    await stage.run(project, "make a deck")

    result = await stage.run(project, "make a deck")

    assert len(composer.seen) == 1, "the second call read the task again"
    assert result.data["reused"]


async def test_a_document_arriving_reopens_the_reading(project: Project, materials: Path):
    stage, composer = _stage(_plan(), _plan())
    await stage.run(project, "make a deck")

    (materials / "extra.md").write_text("# More\n\nAnother source.\n", encoding="utf-8")
    ingest_materials(materials, project.ingest_dir)
    await stage.run(project, "make a deck")

    assert len(composer.seen) == 2


async def test_the_plan_is_recorded_where_a_later_run_finds_it(project: Project, materials: Path):
    stage, _ = _stage(_plan(errands=[{"what": "a photo of the team"}]))

    await stage.run(project, "make a deck")

    recorded = load_plan(intake_path(project))
    assert recorded is not None
    assert recorded.topic == "video segmentation"
    assert recorded.errands[0].what == "a photo of the team"
    assert recorded.digest == deck_state.read(project).digest()


# --- degrading ------------------------------------------------------------


async def test_without_a_model_the_materials_are_still_prepared(project: Project, materials: Path):
    """What is lost is the reading, not the preparation: the conventional directory
    is still ingested and the user is still asked what only they can answer."""
    stage = PrepareStage(composer=None, ingest=ingest_materials)

    result = await stage.run(project, "make a deck")

    assert result.ok
    assert deck_state.read(project).ingested
    assert len(result.data["plan"].questions) == 3
    assert "no model was configured" in " ".join(result.data["plan"].notes)


async def test_a_reply_that_does_not_parse_is_reported_rather_than_guessed(project: Project, materials: Path):
    stage, _ = _stage("I think the materials look fine!", "still not a plan")

    result = await stage.run(project, "make a deck")

    assert not result.ok
    assert "did not parse" in (result.note or "")


async def test_a_reply_cut_short_is_asked_again_with_room_for_the_thinking(project: Project, materials: Path):
    """Three live runs failed this call and succeeded on the author's own retry.

    The gateway sends no `finish_reason`, so `_composer`'s own doubling never fired
    and a truncated reply looked finished. What the caller knows is that the reply
    had to be JSON, so a non-empty one that is not is a reply that stopped early.
    """
    budgets: list[int] = []

    class Recording(FakeComposer):
        async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
            budgets.append(max_tokens)
            return await super().ask(system, parts, max_tokens=max_tokens)

    composer = Recording('{"topic": "a deck", "sta', _plan())
    stage = PrepareStage(composer=composer, ingest=ingest_materials)

    result = await stage.run(project, "make a deck")

    assert result.ok, result.note
    assert result.data["plan"].topic == "video segmentation"
    assert budgets == [stage.max_tokens, stage.max_tokens * 2], "the same budget truncates in the same place"


# --- the reply ------------------------------------------------------------


async def test_the_reply_carries_both_lists_and_asks_the_questions_first(project: Project, materials: Path):
    """The build refuses until the brief exists, so a run that goes fetching first
    spends its errands before finding that out."""
    stage, _ = _stage(_plan(errands=[{"what": "a diagram", "how": "web_search"}]))
    tool = PptPrepareTool(project.workspace, stage)

    body = json.loads(await tool.execute(project="talk", task="make a deck"))

    assert body["ok"]
    assert body["topic"] == "video segmentation"
    assert body["gather"][0]["what"] == "a diagram"
    assert len(body["ask_user"]) == 3
    assert body["next_step"].index("ask_user") < body["next_step"].index("gather")


async def test_a_prohibition_outlives_a_brief_that_could_not_be_written(project: Project, materials: Path):
    """Half a brief is not recorded, so the prohibition the request stated has
    nowhere to live yet -- and the brief is the only thing that carries one forward to
    the builds. Said in the reply, or lost between the two calls."""
    stage, _ = _stage(_plan(stated={"language": "中文", "forbidden": ["别放对比表"]}))
    tool = PptPrepareTool(project.workspace, stage)

    body = json.loads(await tool.execute(project="talk", task="做一份材料，别放对比表"))

    assert load_brief(brief_path(project)) is None
    assert body["forbidden"] == ["别放对比表"]
    assert "别放对比表" in body["next_step"] and "ppt_brief" in body["next_step"]


async def test_a_prepared_deck_is_told_to_go_and_build(project: Project, materials: Path):
    write_brief(
        DeckBrief(language="English", audience="an internal review", pages=PageBudget(10, 12)),
        brief_path(project),
    )
    stage, _ = _stage(_plan())
    tool = PptPrepareTool(project.workspace, stage)

    body = json.loads(await tool.execute(project="talk", task="make a deck"))

    assert "ppt_build" in body["next_step"]
    assert body["figures"] == 1


async def test_a_deck_that_already_has_a_program_is_handed_back_without_being_re_read(
    project: Project, materials: Path, template_file
):
    """The resume branch, which nothing exercised until it broke.

    All four -- brief, plan, outline and program -- have to be on disk before this
    path is taken, so the one existing prepared-deck test writes a brief and goes
    straight past it. That left `_resume_payload` at zero coverage, and what found
    the missing import in it was ruff's F821 rather than a run: an `AttributeError`
    on a resumed layout pass is a whole prepare call thrown away.

    What it must do is hand the build back without asking the model anything, because
    re-reading the task resets the model's attention to preparation and can discard
    the fact that an outline and a runnable program already exist.
    """
    from raven_ppt.backends.script import script_path
    from raven_ppt.services.template import bind

    bind(template_file(where=project.workspace), project)
    write_brief(
        DeckBrief(language="中文", audience="an internal review", pages=PageBudget(10, 12)),
        brief_path(project),
    )
    # The request as well as the topic: resuming skips the reading, so it is only
    # right for the request this project was prepared from, and the plan on disk is
    # what says which that was.
    write_plan(IntakePlan(topic="video segmentation", request="make a deck"), intake_path(project))
    write_outline(
        Outline(takeaway="one model matches four", pages=(PagePlan(page=1, claim="a claim"),)),
        outline_path(project),
    )
    script_path(project).parent.mkdir(parents=True, exist_ok=True)
    script_path(project).write_text("# already written\n", encoding="utf-8")
    stage, composer = _stage(_plan())
    tool = PptPrepareTool(project.workspace, stage)

    body = json.loads(await tool.execute(project="talk", task="make a deck"))

    assert body["ok"] and body["resumed"] is True
    assert composer.seen == [], "a resumed deck must not re-read the task"
    assert body["topic"] == "video segmentation"
    assert body["outline_pages"] == 1
    assert "中文" in body["brief"], "the recorded brief comes back rather than being asked for again"
    assert body["write_the_program_to"] == str(script_path(project).relative_to(project.workspace))
    assert body["template"] == "house-style.pptx", "and the deck it is built inside is still named"
    assert "ppt_build" in body["next_step"]


async def test_path_a_helpers_exist_before_the_prepare_reader_runs(project: Project, materials: Path):
    from raven_ppt.backends.script import asset_helpers, provision

    class CheckingComposer(FakeComposer):
        async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
            assert (project.build_dir / "ppt_layout.py").is_file()
            assert (project.build_dir / "ppt_icons.py").is_file()
            assert (project.build_dir / "ppt_theme.py").is_file()
            return await super().ask(system, parts, max_tokens=max_tokens)

    composer = CheckingComposer(_plan())
    stage = PrepareStage(composer=composer, ingest=ingest_materials)
    tool = PptPrepareTool(
        project.workspace,
        stage,
        provision=lambda deck: provision(deck, asset_helpers()),
    )

    body = json.loads(await tool.execute(project="talk", task="make a deck"))

    assert body["ok"]


async def test_a_project_name_that_is_not_one(project: Project):
    stage, _ = _stage(_plan())
    tool = PptPrepareTool(project.workspace, stage)

    body = json.loads(await tool.execute(project="Not A Slug", task="make a deck"))

    assert not body["ok"]
    assert "not a usable project name" in body["error"]


# --- finding the materials ------------------------------------------------


async def test_the_materials_are_found_by_what_they_are_not_by_what_they_are_called(project: Project, workspace: Path):
    """A name convention was the first answer -- ingest `materials/` -- and it
    misses `papers/`, `素材/` and every other name a user picks."""
    where = workspace / "素材"
    where.mkdir()
    (where / "paper.md").write_text("# A paper\n\nIt reports 91.2 mIoU.\n", encoding="utf-8")
    stage, composer = _stage(_plan())

    result = await stage.run(project, "make a deck")

    assert deck_state.read(project).ingested
    assert "91.2 mIoU" in composer.texts(), "the model was asked before the materials were read"
    assert any("素材" in line for line in result.data["done"])


async def test_two_candidate_directories_are_not_guessed_between(project: Project, workspace: Path):
    """The one case a model is genuinely needed for, and guessing is how the wrong
    paper gets ingested. It sees the same listing this walks."""
    for name, body in (("papers", "# One\n\n91.2 mIoU.\n"), ("notes", "# Two\n\n44 FPS.\n")):
        (workspace / name).mkdir()
        (workspace / name / "doc.md").write_text(body, encoding="utf-8")
    stage, composer = _stage(_plan(materials_dir="notes"))

    await stage.run(project, "the materials are in notes")

    assert "papers/" in composer.texts() and "notes/" in composer.texts()
    assert "44 FPS" in deck_state.read(project).excerpt
    assert "91.2" not in deck_state.read(project).excerpt


async def test_the_directory_the_request_names_beats_the_one_that_was_found(project: Project, workspace: Path):
    """Ingest replaces its three artefacts rather than merging into them, so two
    source directories cannot both be read. If the request points at one, that is
    the one the deck stands on."""
    for name, body in (("stale", "# Old\n\nA number nobody wants: 12.3.\n"),):
        (workspace / name).mkdir()
        (workspace / name / "doc.md").write_text(body, encoding="utf-8")
    stage, _ = _stage(_plan(materials_dir="fresh"))
    (workspace / "fresh").mkdir()
    (workspace / "fresh" / "doc.md").write_text("# New\n\nThe number that matters: 91.2 mIoU.\n", encoding="utf-8")

    result = await stage.run(project, "materials are in fresh")

    excerpt = deck_state.read(project).excerpt
    assert "91.2 mIoU" in excerpt
    assert "12.3" not in excerpt, "the stale directory won"
    assert any("fresh" in line for line in result.data["done"])


async def test_a_named_directory_that_is_not_there_changes_nothing(project: Project, materials: Path):
    """Nothing is refused for it: the deck already holds what was found, and a path a
    model produced out of the task text is the kind that is often simply wrong."""
    stage, _ = _stage(_plan(materials_dir="nowhere"))

    result = await stage.run(project, "materials are in nowhere")

    assert result.ok
    assert "44 FPS" in deck_state.read(project).excerpt


async def test_a_gateway_failure_is_not_reported_as_a_bad_reply(project: Project, materials: Path):
    """A live run against a gateway answering 503 reported "the reply did not
    parse" five times over, which sends the next actor to fix a prompt that was
    never read."""

    class Unavailable:
        failure = "HTTPStatusError: 503 Service Unavailable"

        async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
            return ""

    stage = PrepareStage(composer=Unavailable(), ingest=ingest_materials)

    result = await stage.run(project, "make a deck")

    assert not result.ok
    assert "503" in (result.note or "")
    assert "did not parse" not in (result.note or "")


# --- files the user attached ----------------------------------------------


async def test_a_file_the_user_attached_is_taken_from_outside_the_workspace(project: Project, tmp_path: Path):
    """A channel adapter downloads an attachment under the media cache, outside the
    workspace entirely, so nothing that walks the workspace finds it and the
    containment rule that refuses a model's invented path would refuse it too. It did
    not come from a model."""
    cache = tmp_path.parent / "media-cache"
    cache.mkdir(exist_ok=True)
    attached = cache / "sent-by-the-user.md"
    attached.write_text("# Their paper\n\nIt reports 91.2 mIoU.\n", encoding="utf-8")
    stage, _ = _stage(_plan())

    result = await stage.run(project, "make a deck from this", [str(attached)])

    assert "91.2 mIoU" in deck_state.read(project).excerpt
    assert any("took sent-by-the-user.md as material" in line for line in result.data["done"])
    assert (project.sources_dir / "sent-by-the-user.md").is_file()


async def test_an_attached_pptx_is_the_template_not_the_material(project: Project, template_file):
    """What attaching one means, and it is not something the ingest could read."""
    attached = template_file(name="their-style.pptx")
    stage, _ = _stage(_plan())

    result = await stage.run(project, "use this deck's look", [str(attached)])

    assert deck_state.read(project).template is not None
    assert any("bound their-style.pptx" in line for line in result.data["done"])


async def test_an_attachment_joins_the_sources_rather_than_replacing_them(
    project: Project, materials: Path, tmp_path: Path
):
    """The deck's evidence accumulates. It used to replace, and twice in live runs that
    meant the source the *user supplied* left the evidence while sitting on disk,
    because something arrived after it."""
    stage, _ = _stage(_plan(), _plan())
    await stage.run(project, "make a deck")
    assert "44 FPS" in deck_state.read(project).excerpt

    attached = tmp_path.parent / "more-evidence.md"
    attached.write_text("# And also\n\n78.4 BLEU on DAVIS.\n", encoding="utf-8")
    await stage.run(project, "and this too", [str(attached)])

    state = deck_state.read(project)
    assert "78.4 BLEU" in state.excerpt
    assert "44 FPS" in state.excerpt, "the earlier source left the deck's evidence"
    assert len(state.sources) == 3, "the fixture's two plus the attachment"


async def test_an_attachment_reopens_the_reading(project: Project, materials: Path, tmp_path: Path):
    stage, composer = _stage(_plan(), _plan())
    await stage.run(project, "make a deck")

    attached = tmp_path.parent / "more.md"
    attached.write_text("# More\n\nAnother source.\n", encoding="utf-8")
    await stage.run(project, "and this", [str(attached)])

    assert len(composer.seen) == 2, "the attachment did not reopen the reading"


async def test_a_path_that_is_not_a_file_is_reported_rather_than_ignored(project: Project, materials: Path):
    stage, _ = _stage(_plan())

    result = await stage.run(project, "make a deck", ["/nowhere/at/all.pdf"])

    assert any("is not a file" in line for line in result.data["done"])


async def test_the_models_notes_do_not_crowd_out_the_brief(project: Project, materials: Path):
    """`brief.summary()` is formatted into every design-pass prompt as one line, and
    a live run produced three paragraph-length notes -- two of them restating fields
    the brief already carries."""
    stage, _ = _stage(
        _plan(
            stated={"language": "English", "audience": "a review", "pages_low": 10, "pages_high": 12},
            notes=[f"note {index}" for index in range(9)],
        )
    )

    result = await stage.run(project, "make a deck")

    assert len(load_brief(brief_path(project)).notes) == 3
    assert len(result.data["plan"].notes) == 9, "the plan keeps all of them"


@pytest.mark.asyncio
async def test_a_prepared_project_still_takes_in_a_file_attached_this_turn(
    project: Project, workspace: Path, materials: Path
) -> None:
    """The resume shortcut bypassed the stage unconditionally, so a file attached to a
    later turn was never copied in -- and this tool is the only attachment intake."""
    import json as _json

    from raven_ppt.backends.script import script_path
    from raven_ppt.contracts import (
        DeckBrief,
        Outline,
        PageBudget,
        PagePlan,
        StageResult,
        brief_path,
        intake_path,
        load_plan,
        outline_path,
        write_brief,
        write_outline,
    )
    from raven_ppt.tools.prepare import PptPrepareTool

    write_brief(DeckBrief(language="en", audience="leadership", pages=PageBudget(low=5, high=5)), brief_path(project))
    intake_path(project).parent.mkdir(parents=True, exist_ok=True)
    intake_path(project).write_text(_json.dumps({"topic": "the old topic"}), encoding="utf-8")
    write_outline(Outline(takeaway="t", pages=(PagePlan(page=1, claim="c"),)), outline_path(project))
    script_path(project).parent.mkdir(parents=True, exist_ok=True)
    script_path(project).write_text("# program\n", encoding="utf-8")

    seen: list[tuple[str, tuple[str, ...]]] = []

    class RecordingStage:
        async def run(self, deck, task, files):
            seen.append((task, tuple(files)))
            from raven_ppt.services import state as deck_state

            return StageResult(
                ok=True,
                data={"plan": load_plan(intake_path(deck)), "state": deck_state.read(deck)},
            )

    attached = workspace / "late-arrival.md"
    attached.write_text("# numbers that arrived later\n", encoding="utf-8")

    await PptPrepareTool(workspace, RecordingStage()).execute(
        project=project.slug, task="now use the attached file too", files=[str(attached)]
    )

    assert seen, "the stage was never called, so the attachment was silently dropped"
    assert seen[0][1] == (str(attached),)


@pytest.mark.asyncio
async def test_resuming_a_prepared_project_does_not_raise(project: Project, workspace: Path) -> None:
    """`_resume_payload` read `deck_state` without importing it, so the resume path --
    the whole point of resuming a prepared deck at ppt_build -- raised NameError."""
    import json as _json

    from raven_ppt.backends.script import script_path
    from raven_ppt.contracts import (
        DeckBrief,
        Outline,
        PageBudget,
        PagePlan,
        StageResult,
        brief_path,
        intake_path,
        outline_path,
        write_brief,
        write_outline,
    )
    from raven_ppt.tools.prepare import PptPrepareTool

    write_brief(DeckBrief(language="en", audience="leadership", pages=PageBudget(low=5, high=5)), brief_path(project))
    intake_path(project).parent.mkdir(parents=True, exist_ok=True)
    intake_path(project).write_text(
        _json.dumps({"topic": "the recorded topic", "request": "the recorded topic"}), encoding="utf-8"
    )
    write_outline(Outline(takeaway="t", pages=(PagePlan(page=1, claim="c"),)), outline_path(project))
    script_path(project).parent.mkdir(parents=True, exist_ok=True)
    script_path(project).write_text("# program\n", encoding="utf-8")

    class UnusedStage:
        async def run(self, deck, task, files):
            return StageResult(ok=True)

    reply = await PptPrepareTool(workspace, UnusedStage()).execute(project=project.slug, task="the recorded topic")

    assert json.loads(reply)["ok"] is True


@pytest.mark.asyncio
async def test_a_revised_request_is_read_again_through_the_real_stage(
    project: Project, workspace: Path, materials: Path
) -> None:
    """End to end through PptPrepareTool and a real PrepareStage: the tool's resume and
    the stage's reuse cache are two separate short-circuits, and a revised request has
    to get past both."""
    from raven_ppt.backends.script import script_path
    from raven_ppt.contracts import Outline, PagePlan, intake_path, load_plan, outline_path, write_outline

    stage, composer = _stage(_plan(), _plan())
    tool = PptPrepareTool(workspace, stage)

    await tool.execute(project=project.slug, task="a deck about throughput")

    # Make it a *prepared* project, so the tool's own resume path is live too.
    write_outline(Outline(takeaway="t", pages=(PagePlan(page=1, claim="c"),)), outline_path(project))
    script_path(project).parent.mkdir(parents=True, exist_ok=True)
    script_path(project).write_text("# program\n", encoding="utf-8")

    calls_before = len(composer.seen)
    body = json.loads(await tool.execute(project=project.slug, task="actually make it about cost per deck"))

    assert len(composer.seen) == calls_before + 1, "the revised request never reached the model"
    assert load_plan(intake_path(project)).request == "actually make it about cost per deck"
    assert body["ok"] is True


async def test_a_revised_request_reopens_the_reading(project: Project, materials: Path):
    """The reuse cache is keyed on the inputs' digest, and the request is not in the
    digest -- so the same project answered a new request with the old reading."""
    stage, composer = _stage(_plan(), _plan())
    await stage.run(project, "make a deck about throughput")

    result = await stage.run(project, "actually make it about cost per deck")

    assert len(composer.seen) == 2, "the revised request was answered from the cache"
    assert not result.data.get("reused")
