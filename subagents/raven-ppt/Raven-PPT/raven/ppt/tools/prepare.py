"""`ppt_prepare`: hand over the task, get back a project ready to build in.

The first call of the route, and the only one that reads the user's own words. A
deck task arrives as a sentence -- sometimes carrying everything ("给投资人做一份
15 页中文路演,材料在 ./papers"), sometimes almost nothing -- and everything after
this point works from files rather than from that sentence, so this is where it
gets read.

What comes back is what was *done* plus what is left: the questions only the user
can answer, and the material still to be fetched. Not a verdict on whether the
deck can be made -- that question has one answer on a first pass and costs a model
call to produce it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.backends.script import script_path
from raven.ppt.contracts import (
    Project,
    brief_path,
    intake_path,
    load_brief,
    load_outline,
    load_plan,
    outline_path,
)
from raven.ppt.services import state as deck_state
from raven.ppt.stages.prepare import PrepareStage
from raven.ppt.tools import _return


class PptPrepareTool(Tool):
    name = "ppt_prepare"
    description = (
        "Start a deck: pass the user's request through verbatim and this reads it, then gets the project "
        "ready. It locates the materials the request names and ingests them, takes in any file the user "
        "attached, binds a .pptx the request said to build inside, and when no user template is provided selects "
        "one tagged light template from the bundled defaults. It records the language, audience and "
        "page count the request already states. What it cannot do itself comes back as two lists: "
        "questions to put to the user with ask_user and record with ppt_brief, and material to fetch with "
        "web_search and ppt_fetch. Call it first, and call it again once the material has been fetched."
    )
    timeout_seconds = 600.0

    def __init__(
        self,
        workspace: Path,
        stage: PrepareStage,
        provision: Callable[[Project], Path] | None = None,
    ) -> None:
        self.workspace = workspace
        self.stage = stage
        self.provision = provision

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {
                    "type": "string",
                    "description": "short lowercase slug naming this deck; every later call uses it",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "paths of files the user attached or named this turn, if any. A .pptx is taken as "
                        "the template and everything else as material -- pass them here rather than "
                        "describing them, because an attachment lands outside the workspace where nothing "
                        "else can find it"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "the user's request, in their own words and as complete as you have it. Pass it "
                        "through rather than summarising it: a page count, a language, a directory or an "
                        "instruction you paraphrase away is one this cannot act on"
                    ),
                },
            },
            "required": ["project", "task"],
        }

    async def execute(self, project: str, task: str, files: list[str] | None = None, **kwargs: Any) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        if self.provision is not None:
            self.provision(deck)
        # Only when this turn brought nothing to take in. Resuming skips the stage,
        # and the stage is the only thing that copies an attachment into the project,
        # so resuming past a file is losing it -- there is no second intake.
        resumed = None if files else _resume_payload(deck, project, task)
        if resumed is not None:
            return _return.done(**resumed)
        result = await self.stage.run(deck, task, files or ())
        if not result.ok:
            return _return.failed(result.note or "the task could not be read", hint="try again with the request text")
        if self.provision is not None:
            self.provision(deck)

        plan = result.data["plan"]
        state = result.data["state"]
        payload: dict[str, Any] = {
            "project": project,
            "topic": plan.topic,
            "sources": [_return.where(deck.sources_dir / name, self.workspace) for name in state.sources],
            "figures": len(state.figures),
        }
        if result.data.get("reused"):
            payload["note"] = "nothing about this deck's inputs has changed, so the earlier reading still stands"
        if result.data.get("done"):
            payload["prepared"] = list(result.data["done"])
        if state.brief is not None:
            payload["brief"] = state.brief.summary()
        if state.template is not None:
            payload["template"] = state.template.source.name
        elif state.unbound_templates:
            payload["unbound_pptx"] = list(state.unbound_templates)
        if plan.questions:
            payload["ask_user"] = [question.as_dict() for question in plan.questions]
        if plan.errands:
            payload["gather"] = [errand.as_dict() for errand in plan.errands]
        if plan.notes:
            payload["notes"] = list(plan.notes)
        if plan.stated.forbidden:
            payload["forbidden"] = list(plan.stated.forbidden)
        script = str(script_path(deck).relative_to(self.workspace))
        payload["write_the_program_to"] = script
        return _return.done(asks=_asks(plan, state, script), **payload)


def _resume_payload(deck: Project, project: str, task: str) -> dict[str, Any] | None:
    """Return a build handoff when this is an existing, prepared deck.

    A resumed layout pass must not re-read the user's task or reopen template intake:
    those calls reset the model's attention to preparation and can discard the fact
    that an outline and a runnable program already exist.

    Only for a request this project was already prepared from. Resuming skips the
    reading, so resuming past a revised request answers it with the old plan -- and
    the recorded topic is a reading of the old words, not of these.
    """
    if not (
        load_brief(brief_path(deck))
        and load_plan(intake_path(deck))
        and load_outline(outline_path(deck))
        and script_path(deck).is_file()
    ):
        return None
    recorded = load_plan(intake_path(deck))
    if recorded is not None and recorded.request.strip() != task.strip():
        return None
    state = deck_state.read(deck)
    plan = load_plan(intake_path(deck))
    outline = load_outline(outline_path(deck))
    brief = load_brief(brief_path(deck))
    # The same answer the misses above give, for the same reason: a resume needs all
    # three records, and one of them absent means there is nothing to resume onto. It
    # was an `assert`, which python -O removes -- and what ran then was `plan.topic`
    # against None.
    if plan is None or outline is None or brief is None:
        return None
    payload: dict[str, Any] = {
        "project": project,
        "topic": plan.topic,
        "sources": [_return.where(deck.sources_dir / name, deck.workspace) for name in state.sources],
        "figures": len(state.figures),
        "brief": brief.summary(),
        "outline_pages": len(outline.pages),
        "resumed": True,
        "write_the_program_to": str(script_path(deck).relative_to(deck.workspace)),
    }
    if state.template is not None:
        payload["template"] = state.template.source.name
    return {
        **payload,
        "asks": [
            "this project is already prepared with its brief, outline and build.py; continue directly with ppt_build",
        ],
    }


def _asks(plan: Any, state: Any, script: str) -> list[str]:
    """Every list that still stands, in the order it blocks the deck.

    The questions first, because the build refuses until the brief is recorded and
    a run that goes fetching first spends its errands before finding that out.
    """
    asks: list[str] = []
    if plan.questions:
        asks.append(
            f"put the {len(plan.questions)} question(s) under ask_user to the user with the ask_user tool, "
            "then record the answers with ppt_brief -- the build refuses until the brief exists"
        )
    if plan.stated.forbidden and state.brief is None:
        # The reading found a prohibition and the brief it belongs in could not be
        # written, because some other field of it is still missing. Said here, or it
        # is lost between the two calls: the brief is what carries a prohibition
        # forward to every build, and nothing else does.
        asks.append(
            "the request ruled out " + "; ".join(plan.stated.forbidden) + " -- pass that to ppt_brief as "
            "forbidden=[...] along with the answers, because the brief is what keeps it in front of you "
            "on every build"
        )
    if plan.errands:
        # The sweep, and only the sweep. Searching belongs to `ppt_outline`, which is
        # the first step that knows what a page has to show; asked here it is a
        # question with no target, and one live run answered it with two logos and a
        # marketing banner -- which made the errand look satisfied while the paper
        # and the two benchmark posts its material cited went unswept.
        asks.append(
            f"get the {len(plan.errands)} item(s) under gather by sweeping what the material itself "
            'cites: web_fetch(extractMode="images") on those URLs returns each picture with the caption '
            "its author wrote, which no search result carries. Pass those words to ppt_fetch as its "
            "caption -- nothing downstream can read them off the bytes, so a picture fetched without "
            "them reaches the figure catalogue with nothing but its pixels to say what it is. A paper "
            "cited as an abstract keeps its figures in the PDF, captions and all -- ppt_fetch that and "
            "the ingest reads them off the page. This establishes what there is to choose from; which "
            "picture a page needs is decided against the outline. ppt_fetch what you will use, then "
            "call ppt_prepare again"
        )
    if state.template is None and state.unbound_templates:
        asks.append(
            "there is a .pptx in the workspace this deck is not built in -- bind it with ppt_template if it "
            "is the house style"
        )
    if not asks:
        said = "read the ingested materials" if state.sources else "the deck has no ingested sources to stand on"
        asks.append(f"{said}, then write the program with write_file to {script}, and run ppt_build")
    return asks
