"""`ppt_outline`: decide what the deck argues, before drawing any of it.

The stage the route was missing. An author went from the materials straight to a
python-pptx program, so what each page said got decided while its geometry was being
typed -- and the decks were thin, eight pages of a title and three short lines, with
nothing having asked what the audience must believe by the end.

Deciding that is the author's, not this tool's. What this tool does is bind it and
check the two things about it that can be checked, each of them a whole
build-and-measure cycle earlier than it was being checked before: a figure that is
not in the catalogue, and a page count the brief did not agree. Whether a number
traces to the materials is not among them any more -- see the accepted gap in
`raven/ppt/AGENTS.md`.

And it is where gathering belongs. A page that names what it lacks becomes a search
here, at the one moment when what the deck is missing is actually known --
`ppt_prepare` has to guess it before anything knows what the pages are.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.contracts import (
    Outline,
    PagePlan,
    Project,
    brief_path,
    load_brief,
    outline_path,
    write_outline,
)
from raven.ppt.services import state as deck_state
from raven.ppt.services.gates import material_findings
from raven.ppt.services.ingest import MATERIALS_FILE, stated_chars
from raven.ppt.tools import _return
from raven.ppt.tools._args import ArgumentError, as_objects
from raven.utils.helpers import image_block, text_block

# How many pages one call may plan. Past this the outline is a document rather than
# an argument, and no brief this route accepts asks for more.
MAX_PAGES = 40


class PptOutlineTool(Tool):
    name = "ppt_outline"
    description = (
        "Say what the deck argues, page by page, before you write any of it: the one thing the audience "
        "must believe at the end, and for each page the claim it makes, what carries that claim, which "
        "extracted figures it places, and the supporting points it states. Figures are checked to exist "
        "here rather than after the build, and the page count against the brief. With a template, choose "
        "the nearest editable content prototype for "
        "each page by default; use free composition only when no example can carry its information shape "
        "after editing. A page that names what it still needs comes back as something to "
        "search for -- this is the moment you know what the deck is missing. The build refuses until an "
        "outline is recorded."
    )
    timeout_seconds = 60.0

    def __init__(self, workspace: Path, composer: Any | None = None, views: Any | None = None) -> None:
        self.workspace = workspace
        self.composer = composer
        self.views = views

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_ingest"},
                "takeaway": {
                    "type": "string",
                    "description": (
                        "the one thing the audience has to believe when the deck ends, in a sentence. Every "
                        "page below either establishes it or is not needed"
                    ),
                },
                "pages": {
                    "type": "array",
                    "maxItems": MAX_PAGES,
                    "description": "the pages in order, one entry each",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "page": {"type": "integer", "minimum": 1},
                            "claim": {
                                "type": "string",
                                "description": (
                                    "what this page says, as a statement, and its title. 'Results' is a "
                                    "topic; 'One model matches four task-specific ones' is a claim"
                                ),
                            },
                            "carries": {
                                "type": "string",
                                "description": (
                                    "what carries the claim: a figure, a table, a chart you draw, a single "
                                    "number, a diagram, or prose when it genuinely is prose"
                                ),
                            },
                            "table_plan": {
                                "type": "object",
                                "additionalProperties": False,
                                "description": (
                                    "optional information shape for a table-led page: columns, complete "
                                    "cell rows and the reading cue. This plans a table's information, not "
                                    "a native PowerPoint table or physical coordinates"
                                ),
                                "properties": {
                                    "columns": {"type": "array", "items": {"type": "string"}},
                                    "rows": {
                                        "type": "array",
                                        "items": {
                                            "oneOf": [
                                                {"type": "array", "items": {"type": "string"}},
                                                {"type": "string"},
                                            ]
                                        },
                                    },
                                    "reading": {"type": "string"},
                                },
                            },
                            "figures": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "figure ids this page places, as ppt_ingest listed them",
                            },
                            "says": {
                                "type": "array",
                                "items": {"type": "string"},
                                # Shaped after 238 human-written deck outlines (PresentBench's task
                                # specs, all five domains): 3 points a page at the median, 105 characters
                                # each, ~315 a page, and 74% written as "Label: instruction" with an action
                                # verb -- Compare, Explain, Break down, Highlight, Debunk. Asking for four
                                # to six instead produced five points of 60 characters, which is the same
                                # budget spent on more and thinner labels. 24% of theirs carry their
                                # breakdown rows nested under them, which is what a table page is.
                                # The character figures are where the shape comes from and are not
                                # stated as a target: measured in one box at one size, Chinese fills
                                # it at 400 characters and English at 1168, so a count asked for here
                                # means two different pages. Whether a page holds what it plans is
                                # settled by measuring the render, which is where the copy gets cut.
                                "description": (
                                    "what this page says: the points its claim rests on, each a full "
                                    "sentence, written as `Label: what the page does with it` -- the label "
                                    "is the phrase a reader scans, and the sentence starts with the action: "
                                    "Compare, Explain, Break down, Highlight, Show, Debunk, Recap. "
                                    "Quantities go in verbatim, with their unit and their basis, and beside "
                                    "the thing they are measured against where there is one. Two devices "
                                    "for when the content asks for them and not otherwise: a claim that "
                                    "turns on one figure or one phrase can mark it in `**`, which is what "
                                    "the build sets large; and a point that introduces a breakdown ends on "
                                    "a colon and carries its rows under it, one per line beginning `- `, "
                                    "saying which rows and columns when they are a subset of a source "
                                    "table. A page whose claim is an argument rather than a number wants "
                                    "neither. "
                                    "Enough of them that the claim is carried and no more: the evidence "
                                    "under it, the number beside the judgement, the caveat that makes it "
                                    "honest. Whether the page holds them is settled against the render "
                                    "when it is built, not guessed here; a page carrying two arguments "
                                    "is two pages"
                                ),
                            },
                            "section": {
                                "type": "string",
                                "description": (
                                    "which movement of the deck this page belongs to, named for this "
                                    "material rather than from a template -- the sections of a deck are its "
                                    "own argument. Consecutive pages that develop one movement share the "
                                    "name; a deck of twenty pages usually has eight to twelve of them, so a "
                                    "movement is one or two pages. A deck where every page is its own "
                                    "section has no movement in it, and one section covering half the deck "
                                    "has not been thought through"
                                ),
                            },
                            "needs": {
                                "type": "string",
                                "description": (
                                    "what this page lacks and the materials do not hold, if anything -- it "
                                    "comes back as something to go and get. When it is a picture, say which "
                                    "kind, because they are fetched two different ways: a real thing that "
                                    "exists somewhere (a company's own mark, a product shot, a screenshot, a "
                                    'published plot) is found with web_search(kind="images") and brought in '
                                    "with ppt_fetch, while a diagram nobody has drawn (an architecture, a "
                                    "flow, a timeline, a conceptual figure) is either drawn in the program or "
                                    "made with ppt_generate_image only after searching confirms there is no "
                                    "existing visual to use. A named product, company, published architecture or "
                                    "benchmark is always searched first; generated imagery is limited to a page "
                                    "background or decorative visual and never replaces evidence. Naming it here is what gets it gathered before "
                                    "the page is written rather than after"
                                ),
                            },
                            "prototype": {
                                "type": ["integer", "null"],
                                "description": (
                                    "with a template bound: the example page this page starts from, as "
                                    "ppt_template numbered it. Choose the nearest content example by "
                                    "information shape and adapt its text, pictures and repeated units. "
                                    "Omit only after comparing the available examples and finding that none "
                                    "can carry the page even after deletion, movement or resizing; explain "
                                    "that concrete mismatch in `needs`. A template page is editable, not an "
                                    "immutable form, and free composition is the exception"
                                ),
                            },
                        },
                        "required": ["page", "claim"],
                    },
                },
            },
            "required": ["project", "takeaway", "pages"],
        }

    async def execute(self, project: str, takeaway: str, pages: list[dict[str, Any]], **kwargs: Any) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        brief = load_brief(brief_path(deck))
        if brief is None:
            return _return.failed(
                "no brief recorded, so there is no page budget to plan against",
                hint="record the language, the audience and the length with ppt_brief first",
            )
        if not takeaway.strip():
            return _return.failed("an outline needs to say what the audience must believe at the end")

        try:
            planned = as_objects(pages, "pages")
        except ArgumentError as exc:
            return _return.failed(str(exc), hint='pages: [{"page": 1, "claim": "…", "says": ["…"]}, …]')

        outline = Outline(takeaway=takeaway.strip(), pages=tuple(_plan(entry) for entry in planned))
        state = deck_state.read(deck)
        refusal = _numbering(outline)
        if refusal:
            return _return.failed(refusal, hint="number the pages 1..n in the order they are presented")

        # Re-planned before anything judges it, so every check below sees the outline
        # that will be persisted rather than the one that was submitted. Judging first
        # let a replan rewrite the very fields the checks had just cleared.
        filled = await self._replan(deck, outline, state, brief.language)
        if filled is not None:
            outline, refusal = filled, _numbering(filled)
            if refusal:
                return _return.failed(refusal)

        findings = (
            _missing_figures(outline, state)
            + _structural(outline)
            + _house_pages(outline, state)
        )
        budget = _budget(outline, brief)
        if budget:
            findings.append(budget)
        findings.extend(_thin(deck, brief))
        findings.extend(_thin_pages(outline, state))

        blocking = [finding for finding in findings if finding.severity.value == "blocking"]
        if not blocking:
            write_outline(outline, outline_path(deck))

        payload: dict[str, Any] = {
            "project": project,
            "takeaway": outline.takeaway,
            "pages": [page.summary() for page in outline.pages],
            "figures_placed": list(outline.figures),
            "recorded": not blocking,
        }
        errands = [{"page": page.page, "what": page.needs} for page in outline.pages if page.needs.strip()]
        if errands:
            payload["gather"] = errands
        if findings:
            payload["measured"] = _return.grouped(findings)
        unprototyped = (
            [page.page for page in outline.pages if page.prototype is None]
            if state.template
            else []
        )
        return _return.done(
            blocking=blocking,
            asks=_asks(errands, blocking, bool(state.figures), unprototyped),
            **payload,
        )

    async def _replan(self, deck: Project, outline: Outline, state: Any, language: str = "") -> Outline | None:
        """The plan, written by the second model against the materials. None when it stands.

        Not only the copy. Rewriting `says` under claims that were themselves
        planned thin left the halves disagreeing: a page written up into a
        five-row comparison still carried the errand its one-line version needed,
        because the errand was not the copy's to change. Which page argues what,
        which movement it belongs to, what carries it and what it still needs are
        one decision, and they are made together or not at all.

        What stays the calling agent's: the deck's takeaway and how many pages it
        runs to. Those come from the brief it agreed with the user, and a second
        model is not entitled to either.

        Whenever a second model is configured. It used to run only on a plan
        measured thin, which meant a character count decided whether the deck got
        planned properly -- and that count means different things in different
        languages, so it was deciding on the wrong thing. A reply that does not
        parse leaves the plan exactly as submitted.
        """
        if self.composer is None:
            return None
        materials = _materials(deck)
        if not materials:
            return None
        known = [figure.figure_id for figure in getattr(state, "figures", ()) or ()]
        catalogue = (
            "\n\nFigures already gathered -- assign relevant ones to pages after inspecting their previews:\n"
            + "\n".join(f"- {_figure_summary(figure)}" for figure in getattr(state, "figures", ()) or ())
            if known
            else "\n\nNo figures have been gathered yet, so `figures` is empty on every page."
        )
        draft = "\n".join(
            f"page {page.page}: {page.claim}"
            + (f"  [{page.carries}]" if page.carries else "")
            + (f"  table_plan={page.table_plan}" if page.table_plan else "")
            + (f"  prototype={page.prototype}" if page.prototype is not None else "  prototype=null")
            for page in outline.pages
        )
        parts = [
            text_block(
                f"What the audience must believe at the end: {outline.takeaway}\n\n"
                f"The pages as first drafted, which you are re-planning:\n{draft}"
                f"{catalogue}\n\nThe materials:\n\n{materials}"
            )
        ]
        if self.views is not None:
            for figure in getattr(state, "figures", ()) or ():
                path = deck.figures_dir / getattr(figure, "file", "")
                if not path.is_file():
                    continue
                parts.append(text_block(f"Figure preview {figure.figure_id}: {_figure_summary(figure)}"))
                parts.append(image_block(self.views.data_uri(path)))
        reply = await self.composer.ask(
            REPLAN_BRIEF.format(pages=len(outline.pages), language=language or "the deck's language"),
            parts,
            max_tokens=32000,
        )
        planned = _planned_by_page(reply, known)
        if not planned:
            return None
        return replace(
            outline,
            pages=tuple(
                replace(page, **planned[page.page]) if page.page in planned else page for page in outline.pages
            ),
        )


def _figure_summary(figure: Any) -> str:
    summary = getattr(figure, "summary", None)
    return str(summary()) if callable(summary) else str(getattr(figure, "figure_id", "figure"))


def _planned_by_page(reply: str, known: Sequence[str]) -> dict[int, dict[str, Any]]:
    """{page: fields to replace} out of one reply, or {} when it did not parse.

    Figures are filtered against the catalogue rather than trusted: `figure`
    refuses a plan naming one that does not exist, and a second model inventing an
    id would cost the round for a reason the calling agent could not act on.
    """
    try:
        body = reply[reply.index("{") : reply.rindex("}") + 1]
        pages = json.loads(body).get("pages")
    except (ValueError, AttributeError):
        return {}
    if not isinstance(pages, list):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("page"))
        except (TypeError, ValueError):
            continue
        said = tuple(str(line).strip() for line in (entry.get("says") or []) if str(line).strip())
        planned_table = _table_plan(entry["table_plan"]) if isinstance(entry.get("table_plan"), dict) else None
        if not said and not planned_table:
            continue
        fields: dict[str, Any] = {"says": said}
        for name in ("claim", "carries", "section", "needs"):
            value = str(entry.get(name) or "").strip()
            if value:
                fields[name] = value
        if "prototype" in entry:
            raw_prototype = entry.get("prototype")
            fields["prototype"] = (
                int(raw_prototype)
                if str(raw_prototype or "").strip().isdigit()
                else None
            )
        figures = tuple(str(f).strip() for f in (entry.get("figures") or ()) if str(f).strip() in known)
        fields["figures"] = figures
        if planned_table:
            fields["table_plan"] = planned_table
        out[number] = fields
    return out


def _table_plan(value: dict[str, Any]) -> dict[str, object]:
    """Keep table structure useful while ignoring physical layout instructions."""
    columns = tuple(str(item).strip() for item in value.get("columns") or () if str(item).strip())
    rows: list[object] = []
    for item in value.get("rows") or ():
        if isinstance(item, (list, tuple)):
            cells = tuple(str(cell).strip() for cell in item)
            if any(cells):
                rows.append(cells)
        elif str(item).strip():
            rows.append(str(item).strip())
    reading = str(value.get("reading") or "").strip()
    result: dict[str, object] = {}
    if columns:
        result["columns"] = columns
    if rows:
        result["rows"] = tuple(rows)
    if reading:
        result["reading"] = reading
    return result


def _said_by_page(reply: str) -> dict[int, tuple[str, ...]]:
    """{page: says} out of one reply, or {} when it did not parse.

    A page missing from the reply keeps the plan it had rather than being emptied:
    the caller reads this as "nothing better for that page", which is what a partial
    reply means.
    """
    try:
        body = reply[reply.index("{") : reply.rindex("}") + 1]
        pages = json.loads(body).get("pages")
    except (ValueError, AttributeError):
        return {}
    if not isinstance(pages, list):
        return {}
    out: dict[int, tuple[str, ...]] = {}
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("page"))
        except (TypeError, ValueError):
            continue
        said = tuple(str(line).strip() for line in (entry.get("says") or []) if str(line).strip())
        if said:
            out[number] = said
    return out


def _materials(deck: Project) -> str:
    """The ingested materials, or "" when there are none to read."""
    path = deck.ingest_dir / MATERIALS_FILE
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# Shaped after 238 human-written deck outlines -- PresentBench's task specs across
# academia, economics, education, advertising and talks.
#
# Read per domain, not in aggregate, because the aggregate lies. Of the devices
# below, none is universal and two are habits of particular kinds of material:
#
#   emphasis marks   academia 0%   economics 69%  education 86%  ads 25%  talk 6%
#   nested rows      academia 40%  economics 29%  education 14%  ads  0%  talk 0%
#   colon lead-in    academia 2%   economics 11%  education  1%  ads  0%  talk 1%
#
# So they are offered on a condition -- a claim that turns on one figure, a point
# that introduces a breakdown -- and not required. Required, they would push a
# speech into bolding numbers it does not have. The aggregate figures (42% and 24%)
# are what two data-heavy domains do to a mean, and reading them as "general" is
# the mistake this comment exists to stop. What they have in common,
# and what none of them are, is the whole of this brief:
#
#   * a list of sections, each one or two slides, named for the material's own
#     argument -- never a template's ("Great Reality #3", "The Great Compromiser",
#     "Segment Performance", not "Analysis" or "Details")
#   * 3 points a section at the median, 105 characters each, ~315 a section
#   * 74% written "Label: instruction", the sentence opening on an action verb --
#     include, show, summarise, highlight, explain, compare, break down, debunk
#   * quantities written into the outline verbatim, with unit and basis
#   * 24% nesting their breakdown rows underneath, which is what a table page is
#   * the visual said as intent, not as a file: "a dual timeline showing X against
#     Y", "a performance table, from Table 2"
#
# What is deliberately not here: the derivation of any number, and a worked example
# from the deck in front of the model, which is an example it copies.
# Shaped after 238 human-written deck outlines -- PresentBench's task specs across
# academia, economics, education, advertising and talks. What they have in common
# is the whole of this brief: sections named for the material's own argument and
# never a template's; three points a section at ~105 characters; 74% written
# "Label: instruction" with the sentence opening on an action verb; quantities in
# verbatim with unit and basis; a quarter nesting their breakdown rows underneath,
# which is what a table page is a plan of.
#
# Deliberately absent: the derivation of any number; a worked example from the deck
# in front of the model, which is an example it copies; and any character count as a
# target -- the same count is a full page in Chinese and a third of one in English
# (400 against 1168, measured in one box at one size), so the brief describes what a
# page has to carry and the render decides whether it fits.
REPLAN_BRIEF = """You are planning a {pages}-page deck from the materials. A first draft of
the pages is below; re-plan it. The one thing you may not change is what the audience must
believe at the end, and how many pages there are.

For each page give:

- **section** -- which movement of the deck it belongs to, named for this material's own
  argument, never a generic label. Consecutive pages developing one movement share the name;
  a deck this long usually has eight to twelve of them.
- **claim** -- what the page says, as a statement, and its title. A topic is not a claim.
- **carries** -- what carries it: a figure, a drawn table, a chart, a single number, a diagram, or
  prose when it genuinely is prose.
- **table_plan** -- when `carries` is table-led, give `columns`, complete `rows` and one
  `reading` cue. Each row is an array of cells in the same order as `columns`, so a pure data
  table and a two-product comparison both retain every value. This plans the information
  shape without choosing coordinates or asking for a native PowerPoint table. Omit it for
  non-table pages.
- **says** -- the points its claim rests on, each a full sentence in {language}. Enough of them
  that the claim is carried and no more: the evidence under it, the number beside the
  judgement, the caveat that makes it honest. Whether the page holds them is settled when it
  is built and measured, not here; a page carrying two arguments is two pages rather than one
  long one. Write each as
  **Label: what the page does with it** -- the label is the phrase a
  reader scans, and the sentence after it opens on the action: Compare, Explain, Break down,
  Highlight, Show, Debunk, Recap. A term the deck rests on is said in full where it first
  appears; a result comes with the conditions it was measured under; a quantity is written
  out with its unit and its basis, not referred to. **A page that breaks something down
  carries its rows under their point, one per line beginning `- `** -- that is what a table
  page is a plan of -- when it has one. What makes the rows appear is the sentence *ending on a
  colon* so that it governs them -- a self-contained sentence with the figures run together inside it leaves them
  nowhere to go, which is how a breakdown becomes one long comma-separated line:

      "<label>: <the headline figure>. <the verb introducing the rows>:
      - <row label>: <what this row's cell says>
      - <row label>: <what this row's cell says>"

  When the rows are a subset of a table in the materials, the point says which rows and which
  columns, because otherwise a whole table is copied or an unrecorded selection is invented.

- **one thing to make prominent, when there is one** -- a claim that turns on a single figure
  or a single phrase can wrap it in `**`, inside the sentence where it sits, and the build sets
  that large. The marks are instructions, not characters: nothing prints them. A page arguing a
  position rather than reporting a quantity has nothing to mark, and marking something anyway
  puts the emphasis where the claim is not.

- **figures** -- ids from the catalogue below, and nothing else. Empty when the page places
  none. Inspect the previews before assigning them. A page pairing a figure with text plans
  two to four labelled supporting points on restrained coloured surfaces, with the figure
  dominant and its source or inspected visual caption retained.
- **needs** -- what the page still lacks. When it is a picture, say which kind, because they
  are gathered two different ways: a real thing that exists somewhere -- a company's own
  mark, a product shot, a screenshot, a published plot -- is found with
  web_search(kind="images") and brought in with ppt_fetch; a diagram nobody has drawn -- an
  architecture, a flow, a timeline, a conceptual figure -- is drawn in the program or made
  with ppt_generate_image only after searching finds no existing visual, and only for a page background
  or decorative visual. A named product, company, published architecture or benchmark is always searched
  first; generated imagery never replaces evidence. Empty when the page needs nothing.

- **prototype** -- when a template is bound, choose the nearest example page for every
  content page by default. The page may replace its words and pictures, delete spare
  units, and move or resize regions. Set it to `null` only when no example can carry the
  information shape after those edits, and state the concrete mismatch in `needs`.

Every label and every sentence is written in {language} -- a label in one language and its
sentence in another is not a line a reader can use, and it is what happens when the length
stops being the thing being watched.

The deck is read front to back: a page that restates what an earlier page settled is a page
to spend on what comes next. Numbers, names and dates come from the materials.

Reply as JSON and nothing else, one entry per page, in page order:
{{"pages": [{{"page": 1, "section": "...", "claim": "...", "carries": "...",
"table_plan": {{"columns": [], "rows": [["...", "..."]], "reading": "..."}},
"says": ["..."], "figures": [], "needs": "...", "prototype": null}}, ...]}}"""


def _plan(entry: dict[str, Any]) -> PagePlan:
    return PagePlan(
        page=int(entry.get("page", 0)),
        claim=str(entry.get("claim", "")).strip(),
        carries=str(entry.get("carries") or "").strip(),
        table_plan=_table_plan(entry["table_plan"]) if isinstance(entry.get("table_plan"), dict) else None,
        figures=tuple(str(figure) for figure in entry.get("figures") or ()),
        says=tuple(str(said) for said in entry.get("says") or () if str(said).strip()),
        section=str(entry.get("section") or "").strip(),
        needs=str(entry.get("needs") or "").strip(),
        prototype=int(entry["prototype"]) if str(entry.get("prototype") or "").strip().isdigit() else None,
    )


def _numbering(outline: Outline) -> str:
    if not outline.pages:
        return "an outline with no pages is not an outline"
    numbers = [page.page for page in outline.pages]
    if numbers != list(range(1, len(numbers) + 1)):
        return f"the pages are numbered {numbers}, which is not 1..{len(numbers)} in order"
    empty = [page.page for page in outline.pages if not page.claim]
    if empty:
        return f"page(s) {empty} have no claim; a page with nothing to say is a page to cut"
    return ""


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


def _table_led(page: PagePlan) -> bool:
    carries = page.carries.casefold()
    return any(term in carries for term in ("table", "matrix", "表格", "矩阵", "对照", "定价", "选型表"))


def _thin_pages(outline: Outline, state: Any) -> list:
    """Pages whose whole plan is one line, said while a page is still cheap to merge.

    Structural, not a floor. This used to ask for five points or 140 characters,
    measured off one deck -- and a character count is a different page in every
    language (in one body box at one size, Chinese fills it at 400 and English at
    1168), so the floor asked one language for a page and the other for a third of
    one. Nothing at plan time can tell a page that is short because its figure does
    the talking from a page that is short because nobody planned it -- which is why
    the built page has no floor either.

    What is left is not a judgement about how much: a page with one `says` line, no
    figure, no errand and no prototype has nothing to build from at all, and that is
    true in any language. How full the rest come out is settled by measuring the
    render, where a page that does not fit gets cut and one that does not fill gets
    more.

    A warning, and worded for the case it gets wrong: a table or a chart page carries
    its content in cells and points rather than in `says`, and the plan for one is
    legitimately short. That page needs the figure named or the data said, not more
    bullets, so the message asks for whichever applies rather than assuming.
    """
    from raven.ppt.contracts import Audience, Finding, Severity
    from raven.ppt.services.template.menu import menu, roles

    # A cover, an index and a closing page are meant to be short: the cover of the
    # deck this was measured on holds 44 characters and is not a thin page, it is a
    # cover. They are recognised by the prototype they adapt, which is the same signal
    # `house_page` requires them to name.
    structural = set()
    if state.template is not None:
        structural = {number for number in roles(menu(state.template.source)).values()}

    findings = []
    for page in outline.pages:
        if _table_led(page) and not page.table_plan:
            findings.append(
                Finding(
                    kind="thin_page",
                    severity=Severity.WARNING,
                    page=page.page,
                    audience=Audience.AUTHOR,
                    message=(
                        f"page {page.page} is table-led but has no `table_plan`, so the builder sees prose "
                        "instead of columns and complete cell rows. Record columns, rows[][] and the reading "
                        "cue before layout; the table will be drawn from text boxes and rules, not as an "
                        "Office table"
                    ),
                    detail={"table_plan": False, "carries": page.carries},
                )
            )
        if (
            page.table_plan
            or page.figures
            or page.needs.strip()
            or (page.prototype in structural and page.prototype is not None)
        ):
            continue
        if len(page.says) > 1:
            continue
        findings.append(
            Finding(
                kind="thin_page",
                severity=Severity.WARNING,
                page=page.page,
                audience=Audience.AUTHOR,
                message=(
                    f"page {page.page} plans {len(page.says)} point(s) and nothing else -- no figure, no "
                    f"errand, and not one of the template's own pages. Pages planned like this came out "
                    f"with 26 to 57 characters on them: a heading, one line, and the rest of the page "
                    f"empty. Give it more to say, merge it into its neighbour, or -- if it is a table, a "
                    f"chart or a diagram -- name the figure in `figures` or put the numbers it shows in "
                    f"`says`, because a plan that does not mention them cannot be checked for having them"
                ),
                detail={"says": len(page.says)},
            )
        )
    return findings


def _thin(deck: Project, brief: Any) -> list:
    """The material-per-page warning, repeated where the page count is still cheap.

    `ppt_brief` says it first. It is said again here because this is the last call
    before a program is written, and an author who read it as advice about the brief
    gets it once more as advice about the twelve pages now in front of them.
    """
    return material_findings(_stated_chars(deck), brief)


def _missing_figures(outline: Outline, state: Any) -> list:
    """Figures the plan means to place that the catalogue does not hold."""
    from raven.ppt.contracts import Audience, Finding, Severity

    known = {figure.figure_id for figure in state.figures}
    findings = []
    for page in outline.pages:
        absent = [figure for figure in page.figures if figure not in known]
        if not absent:
            continue
        findings.append(
            Finding(
                kind="figure",
                severity=Severity.BLOCKING,
                page=page.page,
                audience=Audience.AUTHOR,
                message=(
                    f"{', '.join(absent)} is not in this deck's figure catalogue, so there is nothing to "
                    "place. Use an id ppt_ingest listed, or say in `needs` what the page wants and go and "
                    "get it"
                ),
                detail={"absent": absent},
            )
        )
    return findings


# When a quarter of the pages are structure rather than argument, the plan is padded.
# Measured on a real twelve-page plan: three of its pages were section dividers, each
# announcing the claim of the page behind it, all three adapting the same template
# page. That is a quarter of the deck spent saying what the next page then says.
#
# Semantic repetition itself is not measured, and a first attempt that tried came out
# useless: the pair that prompted this -- "四类视频分割任务各有专用模型，部署代价高" and
# "四类任务标注格式各异，催生碎片化专用系统生态" -- shares under a third of its words
# while making one point, so any threshold that caught it flagged half the deck. What
# is measurable is how many pages carry no argument of their own, and that is the
# mechanism behind the repetition rather than a proxy for it.
# A deck this long has an index. Shorter than this and an agenda page is padding --
# five pages do not need a table of contents.
AGENDA_FROM_PAGES = 8
STRUCTURAL_SHARE = 0.25
SAME_PROTOTYPE_IS_STRUCTURAL = 3
# Under this share of pages carrying something to look at, the deck is a document
# read aloud. The same number the built deck is measured against.


def _house_pages(outline: Outline, state: Any) -> list:
    """The cover, the index and the closing page have to be the template's own.

    A requirement rather than advice, and the only one of its kind here: those three
    are the pages a reader recognises whose deck this is by, and a deck that draws its
    own cover announces itself as not theirs before a word of it is read. Blocking at
    outline time is cheap -- nothing has been built yet, and the fix is one field.

    Only for the roles the template actually has. A template with no closing page
    cannot be asked for one, and `roles` only names a page that says what it is.
    """
    from raven.ppt.contracts import Audience, Finding, Severity
    from raven.ppt.services.template.menu import menu, roles

    if state.template is None or not outline.pages:
        return []
    named = roles(menu(state.template.source))
    if not named:
        return []
    first, last = outline.pages[0], outline.pages[-1]
    wanted: list[tuple[str, int, PagePlan | None]] = []
    if "cover" in named:
        wanted.append(("cover", named["cover"], first))
    if "closing" in named:
        wanted.append(("closing", named["closing"], last))
    if "agenda" in named and len(outline.pages) >= AGENDA_FROM_PAGES:
        holds = any(page.prototype == named["agenda"] for page in outline.pages)
        wanted.append(("agenda", named["agenda"], None if holds else first))

    findings = []
    for role, page_number, page in wanted:
        if page is None:
            continue
        if role == "agenda":
            message = (
                f"no page adapts the template's agenda, which is its page {page_number}. A deck of "
                f"{len(outline.pages)} pages has an index, and this template drew one -- give a page "
                f"`prototype: {page_number}`"
            )
            at = None
        elif page.prototype == page_number:
            continue
        else:
            where = "opens" if role == "cover" else "closes"
            message = (
                f"page {page.page} {where} the deck and does not adapt the template's {role}, which is its "
                f"page {page_number}"
                + (f" (it names page {page.prototype})" if page.prototype is not None else "")
                + f". Set `prototype: {page_number}` on it -- the cover, the index and the closing page are how "
                "a reader recognises whose deck this is"
            )
            at = page.page
        findings.append(
            Finding(
                kind="house_page",
                severity=Severity.BLOCKING,
                page=at,
                audience=Audience.AUTHOR,
                message=message,
                detail={"role": role, "template_page": page_number},
            )
        )
    return findings


def _structural(outline: Outline) -> list:
    """Pages that are furniture rather than argument, when there are too many."""
    from collections import Counter

    from raven.ppt.contracts import Audience, Finding, Severity

    if not outline.pages:
        return []
    reused = {
        prototype
        for prototype, count in Counter(page.prototype for page in outline.pages if page.prototype is not None).items()
        if count >= SAME_PROTOTYPE_IS_STRUCTURAL
    }
    furniture = [
        page.page
        for page in outline.pages
        if (page.prototype in reused if page.prototype is not None else False)
        or (len(page.says) <= 1 and not page.figures)
    ]
    if len(furniture) / len(outline.pages) < STRUCTURAL_SHARE:
        return []
    return [
        Finding(
            kind="structural_pages",
            severity=Severity.WARNING,
            audience=Audience.AUTHOR,
            message=(
                f"pages {', '.join(str(number) for number in furniture)} carry no argument of their own -- "
                f"{len(furniture)} of {len(outline.pages)}, which is a quarter of the deck or more. A section "
                "divider that states the claim of the page behind it spends a page twice; either give it "
                "something the next page does not say, or drop it and let the deck's length go to the argument"
            ),
            detail={"pages": furniture, "of": len(outline.pages)},
        )
    ]


def _budget(outline: Outline, brief: Any):
    """Whether the plan is the length that was agreed -- now, not after the build."""
    from raven.ppt.contracts import Audience, Finding, Severity

    count = len(outline.pages)
    if brief.pages.holds(count):
        return None
    direction = "more" if count < brief.pages.low else "fewer"
    return Finding(
        kind="page_budget",
        severity=Severity.BLOCKING,
        audience=Audience.AUTHOR,
        message=(
            f"the outline plans {count} pages and the brief agreed {brief.pages}. It needs {direction} -- "
            "which is a cheap edit here and an expensive one once the program is written"
        ),
        detail={"pages": count, "low": brief.pages.low, "high": brief.pages.high},
    )


def _asks(errands: list, blocking: list, has_figures: bool, unprototyped: list[int] | None = None) -> list[str]:
    asks: list[str] = []
    if blocking:
        asks.append("fix what is refused above and call ppt_outline again -- nothing is recorded until it clears")
        return asks
    if unprototyped:
        asks.append(
            f"pages {', '.join(str(number) for number in unprototyped)} have no template prototype. This "
            "deck has a template, so pick the nearest editable content example for each page with ppt_template. "
            "Keep a page free only when no example can carry its information shape after editing, and record "
            "that concrete mismatch in `needs`"
        )
    if errands:
        asks.append(
            f"get what the {len(errands)} page(s) under gather still need. In parallel, run "
            'web_fetch(extractMode="images") on the source report\'s cited URLs and '
            'web_search(kind="images") for additional candidates; bring selected existing pictures in with '
            "ppt_fetch. Use ppt_generate_image only when both paths find no suitable existing visual. Each "
            "path registers the result in this deck; then call ppt_outline again"
        )
    if not has_figures:
        asks.append(
            "nothing visual was extracted, so every page will be prose or something you draw -- decide which "
            "pages carry a chart, a table or a diagram, and say so in `carries`"
        )
    asks.append("then write the program, one block per page, in the order this outline sets")
    return asks
