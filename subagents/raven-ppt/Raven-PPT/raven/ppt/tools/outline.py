"""`ppt_outline`: decide what the deck argues, before drawing any of it.

The stage the route was missing. An author went from the materials straight to a
python-pptx program, so what each page said got decided while its geometry was being
typed -- and the decks were thin, eight pages of a title and three short lines, with
nothing having asked what the audience must believe by the end.

Deciding that is the author's, not this tool's. What this tool does is bind it and
check what can be checked here, a whole build-and-measure cycle earlier than the same
thing would be caught after a build: a figure that is not in the catalogue, a page
count the brief did not agree, and -- with a template bound -- structural pages that
do not name the template's own. It once also checked a number against an index of
what the materials state; that gate was deleted (design doc D3a: ten findings across
seven runs, all ten false), and no stage builds such an index now.

And it is where gathering belongs. A page that names what it lacks becomes a search
here, at the one moment when what the deck is missing is actually known --
`ppt_prepare` has to guess it before anything knows what the pages are.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.contracts import (
    Outline,
    PagePlan,
    PlannedTable,
    Project,
    brief_path,
    load_brief,
    outline_path,
    write_outline,
)
from raven.ppt.services import citations
from raven.ppt.services import state as deck_state
from raven.ppt.services.gates import material_findings
from raven.ppt.services.ingest import MATERIALS_FILE, stated_chars
from raven.ppt.tools import _return
from raven.ppt.tools._args import ArgumentError, as_objects

# How many pages one call may plan. Past this the outline is a document rather than
# an argument, and no brief this route accepts asks for more.
MAX_PAGES = 40

# The parts a page plays that are not an argument of its own. Not a vocabulary
# invented here: `template.menu._role` reads exactly these four off a template's own
# example pages and calls every other page a content page, `house_style` sorts its
# content pages by the absence of one, and `_house_pages` below already refuses an
# outline whose cover, index and closing do not adapt the template's. The words the
# template side names its pages by are the words a plan names its pages by, or the
# two halves of one decision are stated in two languages.
#
# Restated rather than imported because `raven.ppt.services.template` imports
# python-pptx at module scope while this schema is built wherever the tools are
# registered -- every reference to `menu` in this file is a function-local import for
# that reason. A test holds the two lists against each other so they cannot drift.
PAGE_ROLES = ("cover", "agenda", "section", "closing")


class PptOutlineTool(Tool):
    name = "ppt_outline"
    description = (
        "Say what the deck argues, page by page, before you write any of it: the one thing the audience "
        "must believe at the end, and for each page the claim it makes, what carries that claim, which "
        "extracted figures it places and the supporting points it states. It checks that the figures exist "
        "and that the page count matches the brief; nothing checks a number on a page against the "
        "materials. A page that names what it still needs comes back as something to go and get. The build "
        "refuses until an outline is recorded."
    )
    # A validator's budget, because that is what this call is again. It carried a
    # second model that re-planned all twenty pages inside one reply against the
    # whole material, at a 32k token budget doubled on retry; on a slow backend
    # that never returned inside any budget, and two end-to-end runs died in it --
    # one spending its remaining turns trimming the payload to find a size that fit.
    timeout_seconds = 60.0

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
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
                                    # One shape, not two. `rows` began as row labels -- a string a row --
                                    # and grew into complete cell rows with the string form left in beside
                                    # it, so one plan could mix `["cost", "4.2"]` rows with `"cost"` rows
                                    # and nothing could tell a plan that meant one label from a plan that
                                    # lost its cells. `PlannedTable` decides that shape; this asks for it.
                                    "rows": {
                                        "type": "array",
                                        "items": {"type": "array", "items": {"type": "string"}},
                                        "description": (
                                            "one array a row, its cells in the same order as `columns`, so "
                                            "a row is a row of the grid rather than a label with holes "
                                            "under the other columns"
                                        ),
                                    },
                                    "reading": {"type": "string"},
                                },
                            },
                            "layout": {
                                "type": "string",
                                "description": (
                                    "how this page is composed, as ids from "
                                    "deck/build/references/layouts.md: one or more page structures and "
                                    "the modifier layers stacked on them, `P14 + M4 + M11`. Leave it out "
                                    "for a page cloned from a template example, whose structure is that "
                                    "example's. Naming it here is what makes the choice reviewable "
                                    "before any geometry is written -- and reading the column down the "
                                    "deck is where a deck that composed every page the same way shows"
                                ),
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
                                    "Each point becomes one block on the page and not one bullet in a "
                                    "list: the label is that block's own heading and the sentence is its "
                                    "copy. Quantities go in verbatim, with their unit and their basis. One "
                                    "device, for when the content asks for it: a point that introduces "
                                    "a breakdown ends on a colon and carries its "
                                    "rows under it, one per line beginning `- `, naming which rows and "
                                    "columns when they are a subset of a source table. Enough of "
                                    "them that the claim is carried and no more. Whether the page holds "
                                    "them is settled against the render when it is built, not guessed "
                                    "here; a page carrying two arguments is two pages"
                                ),
                            },
                            "section": {
                                "type": "string",
                                "description": (
                                    "which movement of the deck this page belongs to, named for this "
                                    "material rather than from a template -- the sections of a deck are its "
                                    "own argument. Consecutive pages that develop one movement share the "
                                    "name; a deck of twenty pages usually has eight to twelve of them, so a "
                                    "movement is one or two pages"
                                ),
                            },
                            "role": {
                                "type": "string",
                                "enum": list(PAGE_ROLES),
                                "description": (
                                    "the part this page plays when it is not an argument of its own: `cover` "
                                    "opens the deck, `agenda` indexes it, `section` divides one movement from "
                                    "the next, `closing` ends it. Leave it out for a content page -- a page "
                                    "that argues something of its own is a content page however it is laid "
                                    "out, and most of a deck is content pages. These four are the words a "
                                    "template names its own example pages by, so a page that declares one is "
                                    "saying which of the template's pages it belongs with; and read down the "
                                    "deck they are its rhythm, which is the thing a page-by-page plan is "
                                    "otherwise silent about"
                                ),
                            },
                            # This field is the only trigger for a page-aimed image search, and
                            # the description has to say so or leaving it blank looks free. Across
                            # ten live outlines -- 196 pages -- 148 planned no figure at all, 29
                            # filled `needs`, and 125 were both figureless and silent, so for 63% of
                            # pages the only search that ever ran was `ppt_prepare`'s, which runs
                            # before an outline exists and cannot know what a page has to show. Seven
                            # of the ten left it empty on every page. Not a requirement and not a
                            # gate: a page carrying a chart, a table or prose wants no picture.
                            "needs": {
                                "type": "string",
                                "description": (
                                    "what this page lacks and the materials do not hold, if anything -- it "
                                    "comes back as something to go and get before the page is written "
                                    "rather than after. This field is the switch for the one image search "
                                    "aimed at a page: fill it and the page gets "
                                    'web_search(kind="images") for what it said it needs; leave it blank '
                                    "and the page gets no search of its own -- the only one that ran was "
                                    "the sweep before this outline existed, which could not know what any "
                                    "page would have to show. Blank is the right answer for a page whose "
                                    "claim is carried by a chart you draw, a table or prose, and the wrong "
                                    "one for a page that wants a picture and does not say so. When it is a "
                                    "picture, say which kind: something "
                                    "that exists somewhere (a company's own mark, a product shot, a "
                                    "screenshot, a published plot) is searched for and fetched, while a "
                                    "diagram nobody has drawn is drawn in the program or generated. A named "
                                    "product, company, published architecture or benchmark is always "
                                    "searched first"
                                ),
                            },
                            "prototype": {
                                "type": ["integer", "null"],
                                "description": (
                                    "with a template bound: the example page this page starts from, as "
                                    "ppt_template numbered it. Choose the nearest content example by "
                                    "information shape. Set it to null only after comparing the examples "
                                    "and finding that none can carry the page even after deleting, moving "
                                    "or resizing what is on it, and say what the mismatch was in `needs` -- "
                                    "a null comes back as a question"
                                ),
                            },
                        },
                        "required": ["page", "claim"],
                    },
                },
                "swept": {
                    "type": "array",
                    "description": (
                        "cited URLs you opened and found nothing usable on, one entry each. Needed only "
                        "when ingest extracted no figures and the materials cite pages: no outline is "
                        "recorded while a citation this deck's pictures could have come from is still "
                        "unopened. A URL you bring in with ppt_fetch records itself, so this is for the "
                        "ones that came back with nothing -- including the ones that would not load. It "
                        "is kept with the deck, so a later call does not have to restate it"
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "the page you opened, addressed as the materials cite it",
                            },
                            "found": {
                                "type": "string",
                                "description": (
                                    "what came back, and why none of it is usable here: '404', 'text only, "
                                    "no figures', 'three product screenshots, all UI chrome'. A URL with "
                                    "nothing said about it is not a look"
                                ),
                            },
                        },
                        "required": ["url", "found"],
                    },
                },
            },
            "required": ["project", "takeaway", "pages"],
        }

    async def execute(
        self,
        project: str,
        takeaway: str,
        pages: list[dict[str, Any]],
        swept: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
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
            return _return.failed(
                str(exc),
                hint='pages: [{"page": 1, "claim": "a statement, not a topic", "says": ["Label: what it does"]}]',
            )

        try:
            looked = citations.read(as_objects(swept, "swept"))
        except (ArgumentError, citations.SweepError) as exc:
            return _return.failed(
                str(exc),
                hint='swept: [{"url": "https://...", "found": "text only, no figures"}]',
            )
        citations.record(deck, looked)

        outline = Outline(takeaway=takeaway.strip(), pages=tuple(_plan(entry) for entry in planned))
        declared = _declared_roles(planned)
        state = deck_state.read(deck)
        refusal = _numbering(outline)
        if refusal:
            return _return.failed(refusal, hint="number the pages 1..n in the order they are presented")

        findings = _missing_figures(outline, state) + _structural(outline) + _house_pages(outline, state)
        budget = _budget(outline, brief)
        if budget:
            findings.append(budget)
        findings.extend(_thin(deck, brief))
        findings.extend(_thin_pages(outline, state))
        findings.extend(_table_room(outline))
        findings.extend(_unswept_citations(deck, state))
        findings.extend(_invented_layouts(outline))
        findings.extend(_layout_spread(outline, declared))

        blocking = [finding for finding in findings if finding.severity.value == "blocking"]
        if not blocking:
            write_outline(outline, outline_path(deck))

        payload: dict[str, Any] = {
            "project": project,
            "takeaway": outline.takeaway,
            "pages": [_page_line(page, declared.get(page.page, "")) for page in outline.pages],
            "figures_placed": list(outline.figures),
            "recorded": not blocking,
        }
        errands = [{"page": page.page, "what": page.needs} for page in outline.pages if page.needs.strip()]
        if errands:
            payload["gather"] = errands
        if findings:
            payload["measured"] = _return.grouped(findings)
        unprototyped = [page.page for page in outline.pages if page.prototype is None] if state.template else []
        return _return.done(
            blocking=blocking,
            asks=_asks(errands, blocking, bool(state.figures), unprototyped, outline),
            **payload,
        )


def _table_plan(value: dict[str, Any]) -> dict[str, object]:
    """Keep table structure useful while ignoring physical layout instructions.

    Through `PlannedTable`, so that what is stored is the shape every reader reads.
    While this kept its own copy of that normalisation it let both row forms through
    untouched, which is how a plan mixing them reached the builder. The schema refuses
    a bare string now, and a replan reply is not schema-checked at all, so one arriving
    by that road becomes a one-cell row rather than a row of another type.
    """
    return PlannedTable.of(value).as_dict()


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


def _plan(entry: dict[str, Any]) -> PagePlan:
    return PagePlan(
        page=int(entry.get("page", 0)),
        claim=str(entry.get("claim", "")).strip(),
        carries=str(entry.get("carries") or "").strip(),
        table_plan=_table_plan(entry["table_plan"]) if isinstance(entry.get("table_plan"), dict) else None,
        layout=str(entry.get("layout") or "").strip(),
        figures=tuple(str(figure) for figure in entry.get("figures") or ()),
        says=tuple(str(said) for said in entry.get("says") or () if str(said).strip()),
        section=str(entry.get("section") or "").strip(),
        needs=str(entry.get("needs") or "").strip(),
        prototype=int(entry["prototype"]) if str(entry.get("prototype") or "").strip().isdigit() else None,
    )


def _declared_roles(entries: Sequence[dict[str, Any]]) -> dict[int, str]:
    """{page number: the part it says it plays}, for the pages that say one.

    Read off the submitted entries rather than off the plan because `PagePlan` has no
    field for a role, and a field added to it would reach the file without coming back
    out: `write_outline` stores what the dataclass holds and `load_outline` rebuilds
    only what it names, which is the road `section` is already on -- written into
    `outline.json` on every call and read back as "" on every one. So a role is
    carried as far as the reply that shows it and no further.

    Keyed by page number, which `_numbering` holds to 1..n and which the replan pass
    cannot change: it replaces fields on the pages it is given and never renumbers
    them, so the roles still line up with the pages the reply lists.

    A word outside the four is dropped rather than refused. The schema states them as
    an `enum` and the tool registry validates a call against the schema before this
    runs, so one arriving here came from a caller that skipped that check.
    """
    found: dict[int, str] = {}
    for entry in entries:
        role = str(entry.get("role") or "").strip().casefold()
        if role not in PAGE_ROLES:
            continue
        try:
            found[int(entry.get("page", 0))] = role
        except (TypeError, ValueError):
            continue
    return found


def _page_line(page: PagePlan, role: str) -> str:
    """One page as the reply lists it, and the part it plays where it declared one.

    On the page's own line rather than in a list beside it: what a deck's roles are
    for is the shape of the whole thing, and a column of them running down the pages
    the reply already prints is where that shape is legible in one look.
    """
    return f"{page.summary()}  -- {role}" if role else page.summary()


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
    from raven.ppt.contracts import Finding, Severity
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


# What a table plan is held against, before any of the page it goes on exists. The
# canvas and the safe margin are the grid's own -- `ppt_layout` sets CANVAS_W, CANVAS_H
# = 13.333, 7.5 and MARGIN = 0.72 -- and a test holds these two numbers against that
# module's source so the pair cannot drift apart. The whole safe area rather than the
# body region under a page's heading: what is being asked is whether any page could
# hold this grid, so the generous bound is the honest one and the direction of the
# error is under-reporting.
SAFE_WIDTH_IN = 13.333 - 2 * 0.72
SAFE_HEIGHT_IN = 7.5 - 2 * 0.72
# python-pptx's own cell margins, 0.1in a side -- the same padding `_squeezed_columns`
# adds to a drawn cell's text before it compares it with the column it sits in.
_CELL_PADDING_IN = 0.2
# The leading a drawn table's rows carry over the type in them, from
# `ppt_layout._table_geometry`: a header row is `(size + 12) / 72` inches tall and
# a body row `(size + 10) / 72`.
_HEADER_LEADING_PT = 12
_BODY_LEADING_PT = 10


def _table_room(outline: Outline) -> list:
    """Table plans no page can hold, said before a page is drawn for one.

    A plan for a thirty-row, twelve-column table used to pass this stage untouched and
    meet reality a whole build later as a `wide_table` reading on the drawn result.
    What this stage can honestly say is whether a page has the room for the grid the
    plan describes, and it can say it with the arithmetic the built deck is already
    measured by rather than a second rule of its own.

    That arithmetic is `measure.content._squeezed_columns`, which calls a column
    squeezed when its widest cell needs more than COLUMN_SQUEEZE times the width the
    column has. `ppt_layout` sizes columns in proportion to what they hold, so out of
    a total width W a column gets `W * needs_i / sum(needs)` -- and putting that into
    the squeeze test cancels `needs_i` off both sides, leaving `sum(needs) > W *
    COLUMN_SQUEEZE`. One comparison over the whole plan, holding for every allocation
    the helper can make, and the same test rather than a new one. The tolerance is
    still what it is there: a cell wrapping to a second line is ordinary, and the line
    sits where a cell has lost most of that second line's room.

    Measured at the type floor, which is the smallest the deck may set a cell, so a
    plan that does not fit here does not fit at any size the deck is allowed to use.
    Rows are the same question on the other axis and take no tolerance: a row cannot
    wrap into less height than one line at the floor, so the count times the row
    height is a lower bound on what the grid takes rather than an estimate of it.

    No cap on either count, deliberately. `MAX_TABLE_COLUMNS` was 8 and was deleted
    for counting the wrong thing -- twelve columns of figures fit this canvas and five
    columns of phrases do not, and only a measurement tells those apart. A warning,
    like everything else measured here: which way to answer it is the author's.
    """
    from raven.ppt.contracts import Finding, Severity
    from raven.ppt.services.measure import BODY_FLOOR_PT, COLUMN_SQUEEZE, DEFAULT_MEASURER

    floor = int(BODY_FLOOR_PT)
    carried = SAFE_WIDTH_IN * COLUMN_SQUEEZE
    findings = []
    for page in outline.pages:
        planned = PlannedTable.of(page.table_plan)
        if not planned.width:
            continue
        needed = [0.0] * planned.width
        for line in (planned.columns, *planned.rows):
            for index, cell in enumerate(line):
                needed[index] = max(needed[index], DEFAULT_MEASURER.width(cell, floor) / 72.0 + _CELL_PADDING_IN)
        wide = sum(needed)
        header = (floor + _HEADER_LEADING_PT) / 72 if planned.columns else 0.0
        tall = header + len(planned.rows) * (floor + _BODY_LEADING_PT) / 72
        # A remedy per axis rather than one for both: a plan too tall is not answered
        # by dropping columns, and a tool that says so is a tool the author argues with.
        said = []
        if wide > carried:
            said.append(
                f"the widest cell in each of its {planned.width} columns wants {wide:.1f}in between them, more "
                f"than the {carried:.1f}in that {SAFE_WIDTH_IN:.1f}in of page carries with every cell wrapped "
                f"onto a second line, so drop the columns the claim does not rest on or say it as a chart"
            )
        if tall > SAFE_HEIGHT_IN:
            said.append(
                f"its {len(planned.rows)} rows stand {tall:.1f}in tall against the {SAFE_HEIGHT_IN:.1f}in a "
                f"page has between its margins, so plan the rows the claim rests on and carry the rest onto a "
                f"second page"
            )
        if not said:
            continue
        findings.append(
            Finding(
                kind="table_plan",
                severity=Severity.WARNING,
                page=page.page,
                message=(
                    f"page {page.page} plans a table no page can hold: {'; and '.join(said)}. Measured at the "
                    f"{floor}pt floor the deck may not set type below, so shrinking the type is not one of the "
                    "answers -- and settling it here costs an edit where settling it after the program is "
                    "written costs a rebuild"
                ),
                detail={
                    "columns": planned.width,
                    "rows": len(planned.rows),
                    "width_in": round(wide, 2),
                    "width_room_in": round(carried, 2),
                    "height_in": round(tall, 2),
                    "height_room_in": round(SAFE_HEIGHT_IN, 2),
                },
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
    from raven.ppt.contracts import Finding, Severity

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
                message=(
                    f"{', '.join(absent)} is not in this deck's figure catalogue, so there is nothing to "
                    "place. Use an id ppt_ingest listed, or say in `needs` what the page wants and go and "
                    "get it"
                ),
                detail={"absent": absent},
            )
        )
    return findings


# How many of the outstanding URLs the refusal spells out. Enough that the list is
# the work rather than a sample, short enough that it stays a message; the count
# beside it says how many are not shown.
MAX_LISTED_CITATIONS = 12


def _unswept_citations(deck: Project, state: Any) -> list:
    """Refuse the outline while a cited page nobody opened could hold this deck's pictures.

    The one thing here that is a gate rather than a reading, and it is one because the
    alternative was tried. `ppt_prepare` already adds an errand asking for this sweep
    when ingest extracts nothing; in a live run the errand was sent, sat in the job's
    own `intake.json`, and the author answered it in its second turn -- "a text-based
    competitive analysis with no figures to fetch" -- over material citing 56 URLs, one
    of which serves an architecture diagram with its author's caption on it. Twenty
    pages came out with no image on any of them. An errand is a suggestion, and a
    suggestion is exactly as strong as the author's willingness to take it.

    What makes it safe to refuse is that neither half is a judgement. Ingest extracted
    no figures, which is a count; the materials cite URLs, which is a count. A deck
    whose materials cite nothing is not held, and a deck that has figures is not held,
    so the refusal only ever stands where evidence could exist and nothing looked.

    And it cannot be cleared by saying so. A URL fetched into the deck is recorded by
    the fetch, and a URL that came back with nothing is cleared by naming it and what
    came back -- both of them per-URL, both of them checked against what the materials
    actually cite. There is no ratio here and no sample: the claim being refused is
    that *none* of the citations holds a picture, and the only honest support for it is
    having opened all of them.
    """
    from raven.ppt.contracts import Finding, Severity

    if state.figures:
        return []
    cited = citations.cited(deck)
    if not cited:
        return []
    opened = citations.accounted(deck)
    unswept = [url for url in cited if citations.key(url) not in opened]
    if not unswept:
        return []
    listed = list(unswept[:MAX_LISTED_CITATIONS])
    rest = len(unswept) - len(listed)
    return [
        Finding(
            kind="unswept_citations",
            severity=Severity.BLOCKING,
            message=(
                f"ingest extracted no figures from these materials, and they cite {len(cited)} URL(s) of "
                f"which {len(unswept)} have not been opened. A deck cannot conclude it has nothing to show "
                'from a catalogue nobody filled. Call web_fetch(extractMode="images") on each one -- a '
                "picture on a page a source cites arrives with the caption its author wrote -- and bring "
                "what you will use in with ppt_fetch, or ppt_fetch the PDF behind an abstract so ingest "
                "extracts its figures. For each one that holds nothing usable, or will not load, say so in "
                '`swept`: [{"url": "...", "found": "what came back"}]; it is kept with the deck, so no '
                "later call asks again. Still unopened: "
                + ", ".join(listed)
                + (f" ... and {rest} more" if rest else "")
            ),
            detail={"cited": len(cited), "unswept": len(unswept), "outstanding": listed},
        )
    ]


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
    from raven.ppt.contracts import Finding, Severity
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
                message=message,
                detail={"role": role, "template_page": page_number},
            )
        )
    return findings


# The ids the catalogue actually carries, read out of it rather than written here:
# two lists of the same structures drift, and the file is the one the
# author reads.
_CATALOGUE_ID_RE = re.compile(r"^#{2,4}\s+(P\d+)\b|^\|\s*`(M\d+)`", re.MULTILINE)


def catalogue_ids() -> set[str]:
    """Every page-structure and modifier id `deck/build/references/layouts.md` defines."""
    from raven.ppt.services.assets.script_helpers import REFERENCE_DIRNAME, reference_files

    # The dirname off the writer rather than typed again: `reference_files` keys on
    # the build-directory tail it writes, and a second spelling of it here is a
    # lookup that silently returns nothing the day either one moves.
    text = reference_files().get(f"{REFERENCE_DIRNAME}/layouts.md", "")
    return {found for pair in _CATALOGUE_ID_RE.findall(text) for found in pair if found}


# What counts as an id claim in the `layout` field, in one place because two checks
# read that field now and a second copy of the pattern is how they come to disagree
# about what a page declared.
_LAYOUT_ID_RE = re.compile(r"\b[PMpm]\d+\b")


def _invented_layouts(outline: Outline) -> list:
    """Layout ids the catalogue does not contain.

    A page says how it is composed by naming ids from `deck/build/references/layouts.md`, and
    nothing read that field until this. What one live run wrote into it was `P01`,
    `P04`, `P07` -- zero-padded, and the catalogue's ids are `P1` to `P22`, so not one
    of them was an id at all. Its transcript names layouts.md exactly once, in the
    output of a directory listing: it saw the filename, never opened the file, and
    invented eleven ids that read down the column like a deck with a range of
    structures in it. The pages came out as nine variations on a card grid.

    Refused rather than warned, because the field is optional. Leaving it out says
    "this page is a template clone, or I have not decided"; filling it with an id
    that does not exist says something false about the deck, for free.
    """
    from raven.ppt.contracts import Finding, Severity

    known = catalogue_ids()
    # Nothing to check against, and nothing to refuse for: an empty catalogue means the
    # skill's reference documents are not in this checkout, so the author was never told
    # what an id is. Refusing every declaration here would refuse a field the deck had
    # no way to fill correctly.
    if not known:
        return []
    upper = {one.upper() for one in known}
    invented: dict[int, list[str]] = {}
    for page in outline.pages:
        # Only tokens shaped like an id are read as id claims, and case is not one of
        # them. A looser reading refuses what it was never about: `P11 grid 2x2` was
        # refused over `x2`, and `p14` over its case, both while naming a structure the
        # catalogue carries.
        claimed = _LAYOUT_ID_RE.findall(page.layout)
        unknown = [token for token in claimed if token.upper() not in upper]
        if unknown:
            invented[page.page] = unknown
    if not invented:
        return []
    named = "; ".join(f"page {number}: {', '.join(ids)}" for number, ids in sorted(invented.items()))
    structures = sorted((one for one in known if one.startswith("P")), key=lambda one: int(one[1:]))
    modifiers = sorted((one for one in known if one.startswith("M")), key=lambda one: int(one[1:]))
    return [
        Finding(
            kind="invented_layout",
            severity=Severity.BLOCKING,
            message=(
                f"{named} -- these are not ids in this deck's layout catalogue. It carries "
                f"{structures[0]} to {structures[-1]} as page structures and {modifiers[0]} to "
                f"{modifiers[-1]} as modifier layers, and the ids are not zero-padded. Open "
                "deck/build/references/layouts.md and name what you are actually composing, or leave "
                "`layout` out for a page whose structure is a template example's -- an id that is not "
                "in the file says something about this deck that is not true"
            ),
            detail={"invented": invented, "structures": structures, "modifiers": modifiers},
        )
    ]


def _composition(layout: str) -> str:
    """The ids one page declares, as one comparable string, or "" when it names none.

    Parsed the way `_invented_layouts` parses them, and for the lesson that guard
    already learned: only a token shaped like an id is an id claim, and case is not
    part of one -- a looser reading refused `P11 grid 2x2` over its `x2` and `p14` over
    its case. As a set, because the modifiers stack: `P14 + M4 + M11` and `P14 + M11 +
    M4` are one composition, and their order in the field is not a fact about the page.
    Structures before modifiers and each family by number, so the string reads back the
    way the field is written rather than the way a lexical sort leaves it.
    """
    claimed = {token.upper() for token in _LAYOUT_ID_RE.findall(layout)}
    return " + ".join(sorted(claimed, key=lambda token: (token[0] != "P", int(token[1:]))))


def _layout_spread(outline: Outline, declared: dict[int, str]) -> list:
    """What the declared `layout` column says about the deck, read down the column.

    The cheap half of `measure.variety.layout_variety`, which asks the same question of
    the built file a whole build later. Nothing asked it here until this: the field is
    read in one other place, and `_invented_layouts` only checks that the ids exist --
    so a plan writing `P3` on twelve pages cleared every plan-stage gate and the
    uniformity was discovered off the render, after a build nobody needed to pay for.
    The field's own description says reading the column down the deck is where a deck
    that composed every page the same way shows. This reads it.

    A warning and never a refusal, for the reason the built-deck row already gives:
    "varied enough" is not a property a page has, a series of pages built alike so a
    reader can compare them is good work, and a refusal here would be a refusal of a
    design judgement the measurement is not entitled to make.

    Two readings and never both, because they are different problems asking for
    different edits: a column concentrated on one composition, and a column nobody
    filled in. A deck that has not said how it composes any page is not a deck that
    composed every page alike, and one message for both would ask for the wrong thing.
    """
    from raven.ppt.contracts import Finding, Severity
    from raven.ppt.services.measure.variety import CONCENTRATED, CONCENTRATED_PAGES, MIN_PAGES, _concentration

    # Two sets left out, and the same two the built-deck reading leaves out. The four
    # roles are what a plan calls the template's own furniture -- a cover, an index, a
    # divider, a closing page -- which are meant to be alike, so counting them reports a
    # correct deck for the pages it was told to clone. And a page naming a prototype is
    # composed out of that example rather than out of the catalogue: the field asks it to
    # leave `layout` empty, so reading its silence as "did not decide" would report every
    # template-bound deck. `_references` draws the second line in the same place.
    #
    # Not `_structural`, which answers a different question -- it counts reused
    # prototypes and pages carrying no argument, and a page composed out of the
    # catalogue can be either without that saying anything about its composition.
    composed = [page for page in outline.pages if declared.get(page.page, "") not in PAGE_ROLES and not page.prototype]
    if len(composed) < MIN_PAGES:
        return []
    stated = {page.page: shape for page in composed if (shape := _composition(page.layout))}
    silent = [page.page for page in composed if page.page not in stated]
    listed = ", ".join(str(number) for number in silent)
    if len(silent) >= CONCENTRATED_PAGES and len(silent) / len(composed) >= CONCENTRATED:
        return [
            Finding(
                kind="layout_spread",
                severity=Severity.WARNING,
                message=(
                    f"page(s) {listed} name no layout id -- {len(silent)} of the {len(composed)} pages this "
                    f"deck composes rather than clones -- so there is no column to read, and whether the "
                    f"deck composes every page the same way cannot be answered until it is built and "
                    f"measured. That is a reading and not a verdict: `layout` is optional, and a page whose "
                    f"structure is a template example's has nothing to name here. But a page composed out of "
                    f"the catalogue and silent about it has made the choice without making it reviewable -- "
                    f"open deck/build/references/layouts.md and name the page structure and the modifier "
                    f"layers each of these stacks, which costs an edit here and a rebuild after the program "
                    f"is written"
                ),
                detail={"reading": "undeclared", "pages": silent, "of": len(composed)},
            )
        ]
    found = _concentration(stated)
    if found is None:
        return []
    shape, count = found
    repeated = sorted(number for number, one in stated.items() if one == shape)
    distinct = len(set(stated.values()))
    return [
        Finding(
            kind="layout_spread",
            severity=Severity.WARNING,
            message=(
                f"pages {', '.join(str(number) for number in repeated)} all declare the same composition -- "
                f"{shape} -- which is {count} of the {len(stated)} page(s) that declare one, and the plan "
                f"has {distinct} distinct composition{'' if distinct == 1 else 's'} in it. That is a reading "
                f"and not a verdict: a series of pages built alike so a reader can compare them is good "
                f"work, and material that genuinely wants one shape twice is not a defect. But if those "
                f"pages are not a series, the shape was the path of least resistance rather than a choice -- "
                f"deck/build/references/layouts.md carries a registry of page structures and of modifier "
                f"layers that stack, and more than one modifier on a page is the ordinary case. Answering it "
                f"here costs an edit to this column; the same reading off the built file costs a rebuild"
            ),
            detail={
                "reading": "concentrated",
                "repeated": repeated,
                "composition": shape,
                "declared": len(stated),
                "distinct": distinct,
            },
        )
    ]


def _structural(outline: Outline) -> list:
    """Pages that are furniture rather than argument, when there are too many."""
    from collections import Counter

    from raven.ppt.contracts import Finding, Severity

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
    from raven.ppt.contracts import Finding, Severity

    count = len(outline.pages)
    if brief.pages.holds(count):
        return None
    direction = "more" if count < brief.pages.low else "fewer"
    return Finding(
        kind="page_budget",
        severity=Severity.BLOCKING,
        message=(
            f"the outline plans {count} pages and the brief agreed {brief.pages}. It needs {direction} -- "
            "which is a cheap edit here and an expensive one once the program is written"
        ),
        detail={"pages": count, "low": brief.pages.low, "high": brief.pages.high},
    )


def _references(outline: Outline) -> str:
    """The detail documents, named at the step that decides the author will need them.

    The skill carries the vocabulary and these carry the detail it stands for: the
    twenty-three chart signatures, the table style arguments, the 1304 icon names,
    the shape presets and the formula notation. They are files in the build
    directory rather than context, so an author has to be told to open one -- and
    a plan that has just been recorded is the first moment anything knows which.

    Named, not summarised, and not a checklist: a page carrying none of these needs
    none of them opened. Only the table pages can be pointed at with certainty --
    `table_plan` is structured, while `carries` is a sentence in the deck's own
    language and matching words in it would be guessing.
    """
    lines = [
        "the detail behind the vocabulary is in `deck/build/references/`, written there on every "
        "build: `layouts.md`, `tables.md`, `charts.md`, `icons.md`, `shapes.md`, `formulas.md`. Open the one "
        "for what a page carries before you draw it -- a table drawn without reading `tables.md` gets "
        "the bare default, and the icon names are only in `icons.md`"
    ]
    # `layouts.md` named on its own, and first. The five that were listed here are
    # per-page -- a page carrying no chart needs no `charts.md` -- and this one is not:
    # every page not cloned from a template example is composed out of it. Left inside
    # that list it was read by one model in three, and the two that skipped it composed
    # nine and thirteen pages as the same tinted panel with copy in it. One of them
    # filled the `layout` column with ids that are not in the file.
    composed = [page.page for page in outline.pages if not page.prototype]
    if composed:
        lines.append(
            f"page(s) {', '.join(str(number) for number in composed)} are composed rather than cloned, so "
            "`deck/build/references/layouts.md` is the one this deck needs before any geometry is written: "
            "a registry of page structures and of modifier layers that stack, and more than one modifier on "
            "a page is the ordinary case. Reading it is what the `layout` ids are from"
        )
    tabled = [page.page for page in outline.pages if page.table_plan]
    if tabled:
        lines.append(
            f"page(s) {', '.join(str(number) for number in tabled)} carry a table plan, so "
            "`deck/build/references/tables.md` is one this deck already needs"
        )
    return " -- ".join(lines)


def _asks(
    errands: list,
    blocking: list,
    has_figures: bool,
    unprototyped: list[int] | None = None,
    outline: Outline | None = None,
) -> list[str]:
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
            f"get what the {len(errands)} page(s) under gather still need. Each is a gap left after the "
            "sweep of what the material cites, so this is where a search finally has a target: "
            'web_search(kind="images") for the page\'s stated need. Sweep any cited URL the intake did '
            'not reach with web_fetch(extractMode="images") first -- a picture from a page a source cites '
            "arrives with its own caption. Bring what you select in with ppt_fetch, passing that "
            "caption as its own so the source's words reach the figure catalogue rather than your "
            "reading of the picture, and use "
            "ppt_generate_image only when neither finds a suitable existing visual. Each path registers "
            "the result in this deck; then call ppt_outline again"
        )
    if not has_figures:
        asks.append(
            "nothing visual was extracted, so every page will be prose or something you draw -- decide which "
            "pages carry a chart, a table or a diagram, and say so in `carries`"
        )
    if outline is not None:
        asks.append(_references(outline))
    asks.append("then write the program, one block per page, in the order this outline sets")
    return asks
