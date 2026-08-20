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

from raven.ppt.contracts import (  # noqa: E402
    DeckBrief,
    PageBudget,
    Project,
    brief_path,
    intake_path,
    load_brief,
    load_plan,
    write_brief,
)
from raven.ppt.services import state as deck_state  # noqa: E402
from raven.ppt.services.ingest import ingest_materials  # noqa: E402
from raven.ppt.stages.prepare import BRIEF_QUESTIONS, PrepareStage  # noqa: E402
from raven.ppt.tools.prepare import PptPrepareTool  # noqa: E402


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
    from raven.ppt.services.template import bind

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
    stage, _ = _stage("I think the materials look fine!")

    result = await stage.run(project, "make a deck")

    assert not result.ok
    assert "did not parse" in (result.note or "")


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
