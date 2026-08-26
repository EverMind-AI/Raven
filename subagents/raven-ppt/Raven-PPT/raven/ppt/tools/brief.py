"""`ppt_brief`: record what the deck is for, before anything is drawn.

Three things the materials cannot answer, because the answer is not in them: what
language the audience reads, who they are and on what occasion, and how long the
talk is. A paper is written in English and presented in Chinese; the same results
are a fifteen-minute conference talk or a five-minute internal update; and a page
budget comes from the room, not the paper.

The tool records rather than asks, and that division is deliberate. Where there is
a user to ask, `ask_user` is how you ask them -- one question, three parts -- and
this is where the answer lands. Where there is no user (a scripted run, a batch),
the task text usually says, and an author reading "16-20 slides" out of the brief
it was handed is doing the right thing. What is not acceptable is silence: the
build refuses until a brief exists, because a decision confirmed with a user and
then ignored is worse than never having asked.

Everything recorded here binds something. The page budget is checked against the
built deck, the language against what the pages actually say, and whatever the user
ruled out is quoted back by `ppt_build` on every build -- a prohibition agreed here
and then not repeated is one nothing is holding by the time the pages are drawn. The
audience binds differently: it is not re-quoted per build, it is what the brief the
deck is written against says the deck is for. A field that could not be checked would
have no business being confirmed with a user.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.contracts import DeckBrief, PageBudget, Project, brief_path, write_brief
from raven.ppt.services.gates import material_findings
from raven.ppt.services.ingest import MATERIALS_FILE, stated_chars
from raven.ppt.tools import _return


def _stated_chars(deck: Project) -> int | None:
    """How much the materials say, or None when nothing has been ingested.

    Counted off `materials.md` rather than a record beside it, so a project whose
    materials were replaced by hand is measured as it now reads.
    """
    path = deck.ingest_dir / MATERIALS_FILE
    if not path.is_file():
        return None
    try:
        return stated_chars(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _thin_material(deck: Project, brief: DeckBrief) -> list:
    """Whether the sources say enough for the pages just agreed.

    Nothing ingested yet (a brief recorded before the materials are read) means no
    measurement, not a complaint.
    """
    return material_findings(_stated_chars(deck), brief)


class PptBriefTool(Tool):
    name = "ppt_brief"
    description = (
        "Record what this deck is for: the language the audience reads, who they are and on what "
        "occasion, how many slides the talk has room for, and anything they ruled out. Ask the user "
        "with ask_user first when there is a user to ask -- these are their decisions, not inferences "
        "from the materials -- and read them out of the task when there is not. The build refuses until "
        "this is recorded, because every field reaches the finished deck: the page count and the "
        "language of the copy are checked against it, and the audience and whatever was ruled out are "
        "restated to you on every build."
    )
    timeout_seconds = 30.0

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
                "language": {
                    "type": "string",
                    "description": (
                        "the language the audience reads, in their words -- 中文, English, 日本語. This is "
                        "the language of the deck, which is not necessarily the language of the materials"
                    ),
                },
                "audience": {
                    "type": "string",
                    "description": (
                        "who this is for and on what occasion, in one line: 'a top-tier AI conference oral', "
                        "'an internal engineering review', 'a non-technical exec update'. It decides how much "
                        "the deck may assume, and it is quoted back on every build"
                    ),
                },
                "pages_low": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "fewest slides the talk has room for",
                },
                "pages_high": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ("most slides the talk has room for; the same number as pages_low when it is exact"),
                },
                "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                    "description": "anything else the user asked for, one line each",
                },
                "forbidden": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                    "description": (
                        "what this deck may not use, one thing each: 'no icons', 'no comparison tables', "
                        "'never name a competitor', 'no dark pages'. Only what the user actually ruled out, "
                        "never a preference you inferred. Every build quotes these back in so many words"
                    ),
                },
            },
            "required": ["project", "language", "audience", "pages_low", "pages_high"],
        }

    async def execute(
        self,
        project: str,
        language: str,
        audience: str,
        pages_low: int,
        pages_high: int,
        notes: list[str] | None = None,
        forbidden: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        try:
            brief = DeckBrief(
                language=language,
                audience=audience,
                pages=PageBudget(low=int(pages_low), high=int(pages_high)),
                notes=tuple(note for note in (notes or []) if note.strip()),
                forbidden=tuple(rule.strip() for rule in (forbidden or []) if rule.strip()),
            )
        except (TypeError, ValueError) as exc:
            return _return.failed(str(exc))

        write_brief(brief, brief_path(deck))
        thin = _thin_material(deck, brief)
        return _return.done(
            project=project,
            brief=brief.as_dict(),
            **({"measured": _return.grouped(thin)} if thin else {}),
            asks=[
                *(
                    [
                        f"the materials carry about {thin[0].detail['carries']} page(s) and this brief agreed "
                        f"{brief.pages} -- go and get more material (web_search, then ppt_fetch, then "
                        f"ppt_ingest) or come back with fewer pages, before writing an outline that has to "
                        f"invent the difference"
                    ]
                    if thin
                    else []
                ),
                f"write the deck in {brief.language} for {brief.audience}, in {brief.pages} slides -- "
                "all three are checked against the finished file",
                *(
                    [
                        f"nothing in this deck uses {'; '.join(brief.forbidden)} -- every build says so "
                        "again, and a page that brings one back is a page to rewrite"
                    ]
                    if brief.forbidden
                    else []
                ),
            ],
        )
