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

from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.backends.script import script_path
from raven.ppt.contracts import Project
from raven.ppt.stages.prepare import PrepareStage
from raven.ppt.tools import _return


class PptPrepareTool(Tool):
    name = "ppt_prepare"
    description = (
        "Start a deck: pass the user's request through verbatim and this reads it, then gets the project "
        "ready. It locates the materials the request names and ingests them, takes in any file the user "
        "attached, binds a .pptx the request said to build inside, and records the language, audience and "
        "page count the request already states. What it cannot do itself comes back as two lists: "
        "questions to put to the user with "
        "ask_user, and material to fetch with web_search and ppt_fetch. Call it first, and call it again "
        "after answering either list."
    )
    timeout_seconds = 600.0

    def __init__(self, workspace: Path, stage: PrepareStage) -> None:
        self.workspace = workspace
        self.stage = stage

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

        result = await self.stage.run(deck, task, files or ())
        if not result.ok:
            return _return.failed(result.note or "the task could not be read", hint="try again with the request text")

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
        script = str(script_path(deck).relative_to(self.workspace))
        payload["write_the_program_to"] = script
        return _return.done(asks=_asks(plan, state, script), **payload)


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
    if plan.errands:
        asks.append(
            f'get the {len(plan.errands)} item(s) under gather: web_search(kind="images") for pictures, '
            "ppt_fetch to bring a URL into the project, then call ppt_prepare again"
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
