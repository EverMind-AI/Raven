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
from functools import lru_cache
from pathlib import Path
from typing import Any

from raven.contracts.tool import Tool
from raven_ppt.contracts import (
    Outline,
    PagePlan,
    Project,
    brief_path,
    load_brief,
    outline_path,
    write_outline,
)
from raven_ppt.contracts.outline import layout_fields
from raven_ppt.services import citations
from raven_ppt.services import state as deck_state
from raven_ppt.services.gates import material_findings
from raven_ppt.services.ingest import MATERIALS_FILE, stated_chars
from raven_ppt.tools import _return
from raven_ppt.tools._args import ArgumentError, as_objects

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
# Restated rather than imported because `raven_ppt.services.template` imports
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
                        # Every field but `page` and `claim` may arrive as null: a model that has
                        # nothing to put in an optional field writes null as readily as it leaves
                        # the key out, and both readers (`_plan` here, `load_outline` in the
                        # contracts) already treat the two alike. Declared `string` alone, a
                        # null on one field refused the whole outline, once per page.
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "page": {
                                "type": ["integer", "null"],
                                "minimum": 1,
                                "description": (
                                    "this page's number, 1 for the first; left out or null, the page is "
                                    "numbered by its place in the list"
                                ),
                            },
                            "claim": {
                                "type": "string",
                                "description": (
                                    "what this page says, as a statement, and its title. 'Results' is a "
                                    "topic; 'One model matches four task-specific ones' is a claim"
                                ),
                            },
                            "carries": {
                                "type": ["string", "null"],
                                "description": (
                                    "what carries the claim: a figure, a table, a chart you draw, a single "
                                    "number, a diagram, or prose when it genuinely is prose"
                                ),
                            },
                            "layout": {
                                "type": ["string", "null"],
                                # null in the enum as well as the type: a provider reads these
                                # parameters as JSON Schema, where the two apply together.
                                "enum": [*_structure_enum(), None],
                                "description": (
                                    "which page structure this page is composed on, as one id from "
                                    "deck/build/references/layouts.md -- `P14`. Part 1 of that file is "
                                    "eleven skeletons with every id folded into the one it varies, so "
                                    "the id is the skeleton plus what changed. Leave it empty for a page "
                                    "cloned from a template example, whose structure is that example's -- "
                                    "with a template bound that is most pages, and `prototype` is decided "
                                    "before this. "
                                    "Naming it here is what makes the choice reviewable before any "
                                    "geometry is written -- and reading the column down the deck is where "
                                    "a deck that composed every page the same way shows"
                                ),
                            },
                            "layers": {
                                "type": ["array", "null"],
                                "items": {"type": "string", "enum": _layer_enum()},
                                "description": (
                                    "the modifier layers stacked on that structure, by id from Part 2 of "
                                    'the same file -- `["M4", "M11"]`. The requirement is more than '
                                    "one layer on the region that carries the claim; a page whose only "
                                    "entry is a structure is one tinted rectangle and three points"
                                ),
                            },
                            "anti_pattern": {
                                "type": ["string", "null"],
                                "description": (
                                    "what this page must not turn into, in a line -- the way this "
                                    "structure goes wrong on this page's content. Part 1's third column "
                                    "carries one per skeleton; write the page's own, before the geometry, "
                                    "because by the time the render shows it the page has been drawn"
                                ),
                            },
                            "figures": {
                                "type": ["array", "null"],
                                "items": {"type": "string"},
                                "description": "figure ids this page places, as ppt_ingest listed them",
                            },
                            "says": {
                                "type": ["array", "null"],
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
                                "type": ["string", "null"],
                                "description": (
                                    "which movement of the deck this page belongs to, named for this "
                                    "material rather than from a template -- the sections of a deck are its "
                                    "own argument. Consecutive pages that develop one movement share the "
                                    "name; a deck of twenty pages usually has eight to twelve of them, so a "
                                    "movement is one or two pages"
                                ),
                            },
                            "role": {
                                "type": ["string", "null"],
                                "enum": [*PAGE_ROLES, None],
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
                                "type": ["string", "null"],
                                "description": (
                                    "what this page lacks and the materials do not hold, if anything -- it "
                                    "comes back as something to go and get before the page is written "
                                    "rather than after. This field is the switch for the one image search "
                                    "aimed at a page: fill it and the page gets "
                                    "ppt_image_search for what it said it needs; leave it blank "
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
                                    "with a template bound, the first field to decide for every content "
                                    "page: the example page this page starts from, as ppt_template numbered "
                                    "and captioned it beside each render. Choose the nearest content example "
                                    "by information shape, then adapt it -- replace its words and pictures, "
                                    "delete spare units, move what needs moving. Set it to null only after "
                                    "comparing the examples and the borrowable pages and finding that none "
                                    "can carry the page even so, and say what the mismatch was in `needs`; "
                                    "more than a quarter of the content pages null is refused"
                                ),
                            },
                            "borrowed": {
                                "type": ["string", "null"],
                                "description": (
                                    "the bundled template `prototype` is numbered in, when the page borrows "
                                    "one of the reference pages ppt_template listed under `borrowable_pages` "
                                    "instead of an example of the bound template -- its file name without "
                                    ".pptx, e.g. gold_panel_year_end_summary. Leave it out for the bound "
                                    "template's own pages"
                                ),
                            },
                        },
                        "required": ["claim"],
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

        outline = Outline(
            takeaway=takeaway.strip(), pages=tuple(_plan(entry, position) for position, entry in enumerate(planned, 1))
        )
        # Pages that arrived numbered 1..n but listed out of order are sorted rather than
        # refused: a refusal cost a measured run a two-minute model call to resend the
        # same twenty pages in the order their numbers already stated.
        numbers = [page.page for page in outline.pages]
        reordered = sorted(numbers) == list(range(1, len(numbers) + 1)) and numbers != sorted(numbers)
        if reordered:
            outline = Outline(takeaway=outline.takeaway, pages=tuple(sorted(outline.pages, key=lambda page: page.page)))
        declared = _declared_roles(planned)
        state = deck_state.read(deck)
        refusal = _numbering(outline)
        if refusal:
            return _return.failed(refusal, hint="number the pages 1..n in the order they are presented")

        findings = (
            _missing_figures(outline, state)
            + _borrowed_pages(outline, state)
            + _structural(outline)
            + _house_pages(outline, state)
            + _composed_pages(outline, state)
            + _repeated_prototype(outline, state)
        )
        budget = _budget(outline, brief)
        if budget:
            findings.append(budget)
        findings.extend(_thin(deck, brief))
        findings.extend(_thin_pages(outline, state))
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
        examples_said = _content_examples(state, unprototyped)
        if unprototyped and state.template is not None:
            from raven_ppt.services.template.menu import menu

            examples = [entry for entry in menu(state.template.source) if not entry.role and not entry.hidden]
            suggestions = []
            stranded = []
            for page in outline.pages:
                if page.prototype is None and not page.borrowed:
                    fitting = _suggested_examples(page, examples)
                    if fitting:
                        suggestions.append(f"page {page.page} -> try {', '.join(str(n) for n in fitting)}")
                    else:
                        stranded.append(page)
            if suggestions:
                examples_said += "By what each page says it carries: " + "; ".join(suggestions) + ". "
            # A page no example of this template can carry is the case borrowing exists
            # for, and until now it was the case the reply said least about: it listed
            # the examples that did not fit and left the page composed from scratch.
            borrowed_said = _borrows_for(stranded, state)
            if borrowed_said:
                examples_said += borrowed_said
        # The ask and `repeated_prototype` say the same thing about the same pages, and the
        # finding says it more precisely. `_asks` already returns early for a blocking
        # finding, so this stand-down is belt and braces at the current severity; it is kept
        # because it states the exclusion where the two are chosen rather than leaving it to
        # a control-flow accident one severity change away from speaking twice.
        reused = "" if any(f.kind == "repeated_prototype" for f in findings) else _reused_prototypes(outline, state)
        asks = _asks(
            errands,
            blocking,
            bool(state.figures),
            unprototyped,
            outline,
            examples_said,
            reused,
        )
        if reordered:
            asks.insert(
                0,
                f"the pages arrived listed as {numbers} and were sorted by their numbers; check that order is the one you meant",
            )
        return _return.done(blocking=blocking, asks=asks, **payload)


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
    for position, entry in enumerate(pages, 1):
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("page") or position)
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


def _plan(entry: dict[str, Any], position: int) -> PagePlan:
    # A page that says no number is the page at that place in the list: the numbering
    # check demands 1..n in order anyway, so the number carries nothing the place does
    # not, and a live run wrote every page without one and lost the whole outline.
    return PagePlan(
        page=int(entry.get("page") or position),
        claim=str(entry.get("claim", "")).strip(),
        carries=str(entry.get("carries") or "").strip(),
        **layout_fields(entry),
        figures=tuple(str(figure) for figure in entry.get("figures") or ()),
        says=tuple(str(said) for said in entry.get("says") or () if str(said).strip()),
        section=str(entry.get("section") or "").strip(),
        needs=str(entry.get("needs") or "").strip(),
        prototype=int(entry["prototype"]) if str(entry.get("prototype") or "").strip().isdigit() else None,
        borrowed=str(entry.get("borrowed") or "").strip().removesuffix(".pptx"),
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
    for position, entry in enumerate(entries, 1):
        role = str(entry.get("role") or "").strip().casefold()
        if role not in PAGE_ROLES:
            continue
        try:
            # The same fallback `_plan` takes, so a page that said no number keeps its
            # role at the place the plan gave it rather than filing it under page 0.
            found[int(entry.get("page") or position)] = role
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
    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.services.template.menu import menu, roles

    # A cover, an index and a closing page are meant to be short: the cover of the
    # deck this was measured on holds 44 characters and is not a thin page, it is a
    # cover. They are recognised by the prototype they adapt, which is the same signal
    # `house_page` requires them to name.
    structural = set()
    if state.template is not None:
        structural = {number for number in roles(menu(state.template.source)).values()}

    findings = []
    for page in outline.pages:
        if (
            page.figures
            or page.needs.strip()
            or (page.prototype in structural and page.prototype is not None and not page.borrowed)
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


def _thin(deck: Project, brief: Any) -> list:
    """The material-per-page warning, repeated where the page count is still cheap.

    `ppt_brief` says it first. It is said again here because this is the last call
    before a program is written, and an author who read it as advice about the brief
    gets it once more as advice about the twelve pages now in front of them.
    """
    return material_findings(_stated_chars(deck), brief)


def _borrowed_pages(outline: Outline, state: Any) -> list:
    """Borrowed prototypes that name no bundled template, or no content page of one.

    Refused at the plan, like a figure id the catalogue does not hold: a program written
    against `bundled('gold')` learns the name is wrong one build later, and a plan that
    borrows a cover borrows a page whose one line is the deck's title.
    """
    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.services.template.defaults import bundled_path, reference_pages
    from raven_ppt.services.template.menu import menu

    bound = Path(state.template.source).stem if state.template is not None else ""
    offered = reference_pages(except_stem=bound)
    stems = sorted({stem for stem, _ in offered})
    menus: dict[str, dict[int, Any]] = {}
    findings = []
    for page in outline.pages:
        if not page.borrowed:
            continue
        stem = page.borrowed
        path = bundled_path(stem)
        if path is None or stem == bound:
            what = (
                f"`borrowed: {stem!r}` is the template this deck is built in; its own pages are `prototype` alone"
                if stem == bound
                else f"no bundled template is called {stem!r}; the ones that ship are {', '.join(stems)}"
            )
            findings.append(
                Finding(
                    kind="borrowed", severity=Severity.BLOCKING, page=page.page, message=what, detail={"borrowed": stem}
                )
            )
            continue
        if stem not in menus:
            menus[stem] = {entry.number: entry for entry in menu(path)}
        entry = menus[stem].get(page.prototype or 0)
        pages = ", ".join(str(number) for s, number in offered if s == stem)
        if page.prototype is None:
            what = f"`borrowed: {stem!r}` names a template and no `prototype` names the page; its reference pages are {pages}"
        elif entry is None:
            what = f"{stem} has no page {page.prototype}; its reference pages are {pages}"
        elif entry.role or entry.hidden:
            what = (
                f"{stem} page {page.prototype} is that template's {entry.role or 'hidden'} page, not a content "
                f"page: the deck's own cover, index and closing come from the bound template. Its reference "
                f"pages are {pages}"
            )
        elif (stem, page.prototype) not in offered:
            # The curated list, not "any content page": `reference_pages` holds the pages
            # measured to carry into another template's theme and master. A content page
            # outside it may look fine in its own deck and arrive here with source-specific
            # styling -- the very thing the list was cut to exclude -- so a plan that names
            # one is refused at the plan rather than one build later.
            what = (
                f"{stem} page {page.prototype} is one of that template's own pages and not one of the "
                f"reference pages verified to carry into another template; its reference pages are {pages}"
            )
        else:
            continue
        findings.append(
            Finding(
                kind="borrowed",
                severity=Severity.BLOCKING,
                page=page.page,
                message=what,
                detail={"borrowed": stem, "prototype": page.prototype},
            )
        )
    return findings


def _missing_figures(outline: Outline, state: Any) -> list:
    """Figures the plan means to place that the catalogue does not hold."""
    from raven_ppt.contracts import Finding, Severity

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
    from raven_ppt.contracts import Finding, Severity

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
                "from a catalogue nobody filled. Call web_fetch on each one and take its image links -- a "
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


def _content_examples(state: Any, unprototyped: list[int]) -> str:
    """The template's content examples, each with what it holds, or "".

    Named because the ask that asks for one could not be acted on without them. It
    listed the pages that had no prototype and then said to pick the nearest example,
    leaving the author to work out from the renders which pages those were -- and a
    measured 19-page run left all sixteen content pages free after reading that ask
    twice. The numbers are in the menu this file already opens for `_house_pages`, so
    naming them costs nothing and turns the ask into a choice from a list.

    Shape counts rather than a description: what a page holds is a fact the menu
    carries, and any word for what it *is* would be this file guessing at a design.
    """
    if state.template is None or not unprototyped:
        return ""
    from raven_ppt.services.template.menu import menu

    examples = [entry for entry in menu(state.template.source) if not entry.role and not entry.hidden]
    said = []
    for entry in examples:
        # The same words the bind reply put beside each render, so the two moments
        # agree; shape counts said nothing a plan could match a page to.
        shape = entry.arrangement or ", ".join(
            part
            for part in (
                f"{entry.text_blocks} text blocks" if entry.text_blocks else "",
                f"{entry.pictures} picture(s)" if entry.pictures else "",
                f"{entry.tables} table(s)" if entry.tables else "",
            )
            if part
        )
        note = " (clone only)" if entry.clone_only else ""
        said.append(f"page {entry.number}: {shape}" + (f" ({entry.slots} slots)" if entry.slots else "") + note)
    if not said:
        return ""
    return "This template's content examples are " + "; ".join(said) + ". "


def _suggested_examples(page: PagePlan, examples: list) -> list[int]:
    """Example pages whose materials fit what this page says it carries, by number.

    A suggestion and not a decision: a page carrying a figure is offered the examples
    that show a picture, one carrying a table the examples that hold one, and a page of
    N points the examples with N or more slots. Offered because the ask that said "pick
    the nearest example" was acted on by nobody until a page number stood beside each
    page; the author still chooses, and can still say no example fits.
    """
    return [entry.number for entry in examples if _fits(page, entry)][:4]


def _fits(page: PagePlan, entry) -> bool:
    """Whether this example page can carry what that page says it carries.

    One predicate for the template's own examples and for the reference pages of the
    other bundled templates, because the question is the same one and two spellings of
    it would come to disagree about the page they both offer.
    """
    carries = (page.carries or "").lower()
    wants_table = any(word in carries for word in ("table", "表"))
    if wants_table:
        return bool(entry.tables)
    if _wants_picture(page):
        return bool(entry.pictures)
    # Slots when the page repeats a unit; otherwise its text blocks less the
    # heading, which is what a page of plain boxes offers.
    room = entry.slots or max(entry.text_blocks - 1, 0)
    return room >= max(len(page.says), 2)


def _wants_picture(page: PagePlan) -> bool:
    carries = (page.carries or "").lower()
    return bool(page.figures) or any(word in carries for word in ("figure", "photo", "picture", "image", "图"))


# How many pages on one example page make it a habit rather than a coincidence. Two,
# because the second use is already the moment: by the third the author has settled
# into it. Measured on two live decks built inside bundled templates -- 16 pages on a
# 15-page template and 20 on another 15-page one, every page of both carrying a
# `prototype` and neither carrying a `borrowed` -- so both had to run an example twice
# and both did it silently. Nothing in the reply had ever mentioned that the other
# bundled templates ship pages this deck could borrow, so the repetition was not a
# choice made and lost; it was the only path shown.
PROTOTYPE_REUSED = 2
# How many borrows to name. Past this the ask stops being a shortlist: the reference
# pages of every other bundled template are the pool, and four is a list an author reads
# whole. This template's own unused examples are not capped: that pool is the handful
# of pages the template ships and each is offered once, and a repeating page sent to
# another template while one of them still fits is the outcome the offer exists to stop.
MOST_BORROWS_OFFERED = 4


def _borrowable_offers(state: Any) -> list:
    """(stem, number, menu entry) for every reference page this deck may borrow.

    Ordered one template at a time round the templates rather than all of one and then
    all of the next. The order is the order the offers are picked in, and the reference
    pages are declared eleven of one template first: picked in that order, a deck
    repeating four examples was offered four pages of the same borrowed template, which
    answers a monoculture with a monoculture.
    """
    grouped: dict[str, list] = {}
    for stem, number, entry in _reference_menu(Path(state.template.source).stem if state.template else ""):
        grouped.setdefault(stem, []).append((stem, number, entry))
    offers = []
    for index in range(max((len(pages) for pages in grouped.values()), default=0)):
        for pages in grouped.values():
            if index < len(pages):
                offers.append(pages[index])
    return offers


@lru_cache(maxsize=8)
def _reference_menu(bound_stem: str) -> tuple:
    """The reference pages and what each holds, read off the bundled templates once.

    Opens seven .pptx and takes about two seconds, and two places in one reply want it,
    so it is cached rather than paid for twice. Keyed by the bound template because
    that is what decides which templates are opened; the payload is package data and
    does not change while a process runs.
    """
    from raven_ppt.services.template.defaults import bundled_path, reference_pages
    from raven_ppt.services.template.menu import menu

    menus: dict[str, dict[int, Any]] = {}
    found = []
    for stem, number in reference_pages(except_stem=bound_stem):
        if stem not in menus:
            path = bundled_path(stem)
            menus[stem] = {entry.number: entry for entry in menu(path)} if path else {}
        entry = menus[stem].get(number)
        if entry is not None and not entry.role and not entry.hidden:
            found.append((stem, number, entry))
    return tuple(found)


def _borrow_caveat(stem: str, number: int) -> str:
    """What this page needs doing to it, or "" when it needs nothing.

    One thing, measured over the 252 cross-template clones of these pages: four of
    them carry drawings painted in their own template's accents, and a bitmap is the
    one thing on a reference page that nothing recolours. Every other borrow came
    across in the deck's own palette, and a caveat on one of those is what teaches an
    author to read past the caveats.
    """
    from raven_ppt.services.template.defaults import reference_artwork

    drawings = reference_artwork(stem, number)
    if not drawings:
        return ""
    return f" (replace its {drawings} drawing(s), painted in {stem.split('_')[0]}'s own colours)"


def _pale_accent_note(state: Any) -> str:
    """The sentence a deck whose accent cannot carry white type needs, once.

    Once for the deck rather than once per offer: it is a fact about this template and
    not about any page, it applies to every page borrowed into it, and repeated beside
    each offer it reads as boilerplate -- which is how a caveat stops being read.
    """
    from raven_ppt.services.template.theme import borrow_ink_note

    if state.template is None:
        return ""
    said = borrow_ink_note(state.template.inventory, Path(state.template.source))
    return f" {said}" if said else ""


def _borrows_for(pages: list, state: Any) -> str:
    """Reference pages for the pages no example of the bound template can carry.

    The other half of the same offer `_reused_prototypes` makes, at the other moment:
    there an example fits and has been used already, here no example fits at all.
    """
    if not pages or state.template is None:
        return ""
    offers = _borrowable_offers(state)
    if not offers:
        return ""
    named, matched = _matched_borrows(pages, offers)
    if not named:
        return ""
    return (
        "No example of this template fits "
        + ", ".join(f"page {number}" for number in matched)
        + ", which is what borrowing is for -- a reference page of another bundled template arrives in this "
        "deck's own colours and master with only its arrangement carried across: "
        + "; ".join(named)
        + ". Record `borrowed: '<template>'` and `prototype: N` on that page."
        + _pale_accent_note(state)
        + " "
    )


def _matched_borrows(pages: list, offers: list) -> tuple[list[str], list[int]]:
    """One reference page per plan page it fits, and the plan pages that got one.

    A reference page is offered once: two pages of a deck built on one arrangement is
    the repetition this exists to answer, so offering the same page twice would answer
    it with itself.
    """
    named: list[str] = []
    matched: list[int] = []
    taken: set[tuple[str, int]] = set()
    for page in pages:
        pick = next(((s, n, e) for s, n, e in offers if _fits(page, e) and (s, n) not in taken), None)
        if pick is None:
            continue
        stem, number, entry = pick
        taken.add((stem, number))
        shape = entry.arrangement or f"{entry.text_blocks} text blocks"
        slots = f", {entry.slots} slots" if entry.slots else ""
        named.append(f"page {page.page} -> {stem} page {number} ({shape}{slots})" + _borrow_caveat(stem, number))
        matched.append(page.page)
        if len(named) >= MOST_BORROWS_OFFERED:
            break
    return named, matched


# How many pages in a row on one example read as a series rather than a habit. Five case
# pages built alike so a reader compares them across is the deck this was written against,
# and two adjacent pages on one example is the ordinary repetition the ask is about; at
# three in a row "built alike on purpose" is the likelier reading, and the ask says so
# rather than pulling one page out of the middle of it.
SERIES_RUN = 3


def _reused_prototypes(outline: Outline, state: Any) -> str:
    """The ask that names another page for each page repeating an example.

    An ask rather than a finding: a template with fewer example pages than the deck
    has pages *has* to repeat one, so repetition is not an error and refusing it would
    refuse the decks this is meant to help. What the author is missing is not a rule,
    it is the alternative, by number, so it is a choice from a list rather than a thing
    to go and look up.

    This template's own unused examples come first, and the other templates' reference
    pages only for a page none of those fits. Measured on a delivered deck: twenty pages
    on ten of the template's eleven content examples, one example five times and another
    three, while five examples -- among them the one with the largest picture slot the
    template draws, 35% of the page -- started nothing; the borrow offer alone would have
    sent the author to another template past the pages it already had. The template's
    own furniture (cover, index, divider, closing) repeats by design and is not counted.
    """
    from collections import Counter

    from raven_ppt.services.template.menu import menu

    if state.template is None or not outline.pages:
        return ""
    entries = menu(state.template.source)
    house = {entry.number for entry in entries if entry.role}
    counted = [
        page for page in outline.pages if page.prototype is not None and (page.borrowed or page.prototype not in house)
    ]
    used = Counter((page.borrowed, page.prototype) for page in counted)
    repeated = {source for source, count in used.items() if count >= PROTOTYPE_REUSED}
    if not repeated:
        return ""
    runs = _series(counted)
    in_series = {number for run in runs for number in run}
    # The first page on an example keeps it; the ones after it are the ones with
    # somewhere else to go -- unless they sit inside a series.
    seen: set = set()
    repeating = []
    for page in counted:
        key = (page.borrowed, page.prototype)
        if key in repeated and key in seen and page.page not in in_series:
            repeating.append(page)
        seen.add(key)
    if not repeating:
        return ""
    taken = {page.prototype for page in outline.pages if page.prototype is not None and not page.borrowed}
    examples = [entry for entry in entries if not entry.role and not entry.hidden]
    unused = [entry for entry in examples if entry.number not in taken]
    own_named, own_matched = _matched_examples(repeating, unused, examples, state)
    left = [page for page in repeating if page.page not in own_matched]
    borrow_named: list[str] = []
    borrow_matched: list[int] = []
    if left:
        offers = _borrowable_offers(state)
        if offers:
            borrow_named, borrow_matched = _matched_borrows(left, offers)
    if not own_named and not borrow_named:
        return ""
    ordered = sorted(repeated, key=lambda one: (one[0], one[1]))
    numbers = ", ".join(f"{one[1]}" if not one[0] else f"{one[0]} page {one[1]}" for one in ordered)
    beyond_first = sum(count - 1 for count in used.values() if count >= PROTOTYPE_REUSED)
    said = (
        f"prototype{'s' if len(ordered) > 1 else ''} {numbers} each start more than one of this deck's "
        f"{len(outline.pages)} pages ({beyond_first} page(s) repeat one). "
        "Repeating an example is not wrong -- a template with fewer example pages than the deck has pages has to"
    )
    if runs:
        series = " and ".join(f"pages {run[0]}-{run[-1]}, in a row on {_source_said(counted, run[0])}," for run in runs)
        said += (
            f" -- and {series} read as a series built alike so a reader can compare them, so they are left alone here"
        )
    said += ". "
    if own_named:
        said += (
            f"But {len(unused)} of this template's {len(examples)} content examples start no page of this deck. "
            "By what each repeating page says it carries: "
            + "; ".join(own_named)
            + ". Record `prototype: N` on that page and clone it with `clone_page(prs, prototype(tpl, N))`, "
            "then `replace_text` per line. "
        )
    if borrow_named:
        said += (
            f"For page{'s' if len(borrow_matched) > 1 else ''} {', '.join(str(number) for number in borrow_matched)} "
            "this template has no unused example left that fits, and the other bundled templates ship reference "
            "pages this deck can borrow -- a "
            "borrowed page arrives in this deck's own colours and master with only its arrangement carried across: "
            + "; ".join(borrow_named)
            + ". Borrow one with `clone_page(prs, prototype(bundled('<template>'), N))` and record "
            "`borrowed: '<template>'` and `prototype: N` on that page of the plan." + _pale_accent_note(state)
        )
    return said.rstrip()


def _series(pages: list) -> list[list[int]]:
    """Runs of at least SERIES_RUN consecutive page numbers on one example, as page numbers."""
    runs: list[list[int]] = []
    run: list[int] = []
    previous = None
    for page in sorted(pages, key=lambda one: one.page):
        key = (page.borrowed, page.prototype)
        if previous is not None and key == previous[0] and page.page == previous[1] + 1:
            run.append(page.page)
        else:
            if len(run) >= SERIES_RUN:
                runs.append(run)
            run = [page.page]
        previous = (key, page.page)
    if len(run) >= SERIES_RUN:
        runs.append(run)
    return runs


def _source_said(pages: list, number: int) -> str:
    page = next(one for one in pages if one.page == number)
    return f"{page.borrowed} page {page.prototype}" if page.borrowed else str(page.prototype)


def _matched_examples(pages: list, unused: list, examples: list, state: Any) -> tuple[list[str], list[int]]:
    """One unused example of this template per plan page it fits, and the pages that got one.

    Each example offered once, as `_matched_borrows` offers a reference page once, and
    for the same reason; unlike the borrows, every page one fits gets its offer (see
    MOST_BORROWS_OFFERED). The line carries what the author chooses by and the menu line
    does not put beside a number: the picture slots with their share of the page, and
    which of them is the largest the template draws.
    """
    canvas = state.template.inventory.width_in * state.template.inventory.height_in if state.template else 0.0
    largest = max((area for entry in examples for _, area in _picture_areas(entry)), default=0.0)
    named: list[str] = []
    matched: list[int] = []
    taken: set[int] = set()
    for page in pages:
        # A page carrying a picture is offered the roomiest slot first: the deck this was
        # written against put five photographs on the example with a 16% slot while the
        # 35% one, listed six pages later, started nothing.
        candidates = sorted(unused, key=lambda entry: -max((area for _, area in _picture_areas(entry)), default=0.0))
        if not (page.figures or _wants_picture(page)):
            candidates = unused
        pick = next((entry for entry in candidates if _fits(page, entry) and entry.number not in taken), None)
        if pick is None:
            continue
        taken.add(pick.number)
        shape = pick.arrangement or f"{pick.text_blocks} text blocks"
        slots = f", {pick.slots} slots" if pick.slots else ""
        named.append(
            f"page {page.page} -> this template's page {pick.number} ({shape}{slots}{_slot_facts(pick, canvas, largest)})"
        )
        matched.append(page.page)
    return named, matched


_SLOT = re.compile(r"\[(\d+)\] ([\d.]+)x([\d.]+)in (\S+)")


def _picture_areas(entry: Any) -> list[tuple[str, float]]:
    """("WxHin", square inches) for each of the entry's picture slots that is not an icon."""
    found = []
    for place in getattr(entry, "picture_slots", ()) or ():
        match = _SLOT.match(place)
        if match is None or match.group(4) == "icon":
            continue
        found.append((f"{match.group(2)}x{match.group(3)}in", float(match.group(2)) * float(match.group(3))))
    return found


def _slot_facts(entry: Any, canvas: float, largest: float) -> str:
    """The picture slots as a choice reads them: size, share of the page, and the largest."""
    areas = _picture_areas(entry)
    icons = sum(1 for place in getattr(entry, "picture_slots", ()) or () if place.endswith(" icon"))
    parts = []
    if areas:
        # Identical slots are counted rather than listed. A template page with fifteen
        # equal thumbnails spent about 700 characters of one ask repeating "2.3x2.6in, 6%
        # of the page and" -- inside the one ask that had to be read, in the run that did
        # not read it.
        counted: dict[str, int] = {}
        for size, area in areas:
            share = f"{size}, {area / canvas:.0%} of the page" if canvas else size
            if largest and area >= largest:
                share += ", the largest picture slot this template draws"
            counted[share] = counted.get(share, 0) + 1
        shares = [share if count == 1 else f"{count} of {share} each" for share, count in counted.items()]
        parts.append(f"picture slot{'s' if len(areas) > 1 else ''} " + " and ".join(shares))
    if icons:
        parts.append(f"{icons} icon slot{'s' if icons > 1 else ''}")
    return f"; {'; '.join(parts)}" if parts else ""


# The most of a deck's content pages that may be composed from scratch while a template
# is bound. Measured on two decks in the same template family: the one a reader called
# the template's own started 12 of its 20 pages from an example page and composed 4 of
# its 17 content pages (24%); the one called "nothing but self-drawn layouts" composed
# 17 of 17, each with a line in `needs` saying an example would have cost more to adapt
# than to redraw. The ask that listed those pages had been read twice and changed
# nothing, so the share is a refusal now. A quarter rather than a third: the deck read as
# the template's composed 3 of 17 (18%); a later one composed 6 of 17 (35%) and its six
# composed pages -- two tables, a card grid, a flow row, a timeline, a risk grid -- were
# the pages the reader pointed at as drawn by hand and ugly, and a third would have let
# that plan through. A quarter still leaves a comparison table and a chart page.
COMPOSED_SHARE = 1 / 4
# The most of a deck's content pages one prototype may start before the reply says so.
# The mirror of COMPOSED_SHARE by subject -- that one is about a plan that leaves the
# template, this one about a plan that uses one page of it for the whole deck -- but not
# by severity: composing is refused, repeating is reported, because a user can ask for a
# uniform deck and a refusal would leave that run unable to finish. Measured on the outlines of three
# live runs. A 14-page run put 8 of its 11 content pages on one prototype (73%) and its
# renders are the finding: 8 of the 14 pages are the same five-hexagon card row, and the
# author had been handed a per-page shortlist of unused examples twice -- it moved the
# repeated prototype from 6 to 5 between the two calls and kept the eight. Two 16-page
# runs put at most 2 of their 14 content pages on one prototype (14%), and a 12-page run
# started 10 pages on 10 different ones. So the live spread is one or two pages per
# prototype against one deck at eight, and a strict majority separates them without
# sitting near either.
PROTOTYPE_SHARE = 1 / 2
# Below this many pages on one prototype only the ask speaks, whatever the share: a
# 4-page deck whose two content pages share an example is not a monotonous deck, and
# SERIES_RUN already says three pages built alike reads as a series built on purpose
# rather than a habit. So this starts one page past that, and the share decides from there.
PROTOTYPE_FLOOR = SERIES_RUN + 1


def _composed_pages(outline: Outline, state: Any) -> list:
    """Refuse a plan that composes most of its content pages from scratch inside a template.

    Only when a template is bound, and only over the content pages: the cover, index,
    dividers and closing are `_house_pages`'s business. A page with a `prototype` or a
    `borrowed` reference page starts from a designed composition; one with neither is
    composed, and more than COMPOSED_SHARE of them composed is a deck that has stopped
    being the template's, whatever house style its geometry is measured against. The
    finding names the template's content examples, because the one thing the earlier
    ask lacked was a list to choose from.
    """
    from math import ceil

    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.services.template.menu import menu

    if state.template is None or not outline.pages:
        return []
    entries = menu(state.template.source)
    house = {entry.number for entry in entries if entry.role}
    content = [page for page in outline.pages if not (page.prototype in house and not page.borrowed)]
    if not content:
        return []
    composed = [page.page for page in content if page.prototype is None and not page.borrowed]
    allowed = ceil(len(content) * COMPOSED_SHARE)
    if len(composed) <= allowed:
        return []
    examples = [entry.number for entry in entries if not entry.role and not entry.hidden]
    listed = ", ".join(str(number) for number in composed)
    # The refusal used to end at "or a `borrowed` page from `borrowable_pages`", which
    # is the sentence a composed page was already refused past: it names a category and
    # leaves the author to go and look up which page of which template carries this
    # page's shape. So the same matcher the asks use is run here, and when it finds
    # nothing the clause is absent rather than vague.
    plans = [page for page in content if page.prototype is None and not page.borrowed]
    named, matched = _matched_borrows(plans, _borrowable_offers(state))
    borrowable = (
        (
            " Of those, these pages of the other bundled templates carry what the page says it carries, and a "
            "borrowed page arrives in this deck's own colours and master with only its arrangement carried "
            "across -- record `borrowed: '<template>'` and `prototype: N` on it: " + "; ".join(named) + "."
        )
        + _pale_accent_note(state)
        if named
        else ""
    )
    return [
        Finding(
            kind="composed_pages",
            severity=Severity.BLOCKING,
            message=(
                f"{len(composed)} of this deck's {len(content)} content pages ({listed}) plan to be composed from "
                f"scratch, and at most {allowed} may be while a template is bound: the template offers "
                f"{len(examples)} content examples (pages {', '.join(str(n) for n in examples)}). "
                "Give each of the rest a `prototype` -- the nearest example by "
                "information shape, then replace its text and pictures, delete spare units and move what needs "
                "moving; a prototype is a starting composition, not a form to fit. "
                "Keep composing only the pages whose shape no example carries, and say which "
                "shape that was in `needs`." + borrowable
            ),
            detail={
                "composed": composed,
                "allowed": allowed,
                "content_pages": len(content),
                "examples": examples,
                **({"borrowable_for": matched} if matched else {}),
            },
        )
    ]


def _repeated_prototype(outline: Outline, state: Any) -> list:
    """Say that a plan starts most of its content pages on one prototype.

    A refusal. It was a warning for one revision, on the argument that a uniform deck is
    a thing a user can ask for and that refusing would trap a run told to repeat a layout.
    What settled it is that no run has asked: three plans on briefs carrying no layout
    instruction came out at 8 of 11, 5 of 9 and 10 of 10 content pages on one prototype,
    the last with 26 of the template's 27 examples untouched, and each was corrected
    within a minute of being refused. A warning would have filed all three as proposed.
    The refusal names a page to move each repeat to, which is what keeps it a gate the
    author can act on rather than a wall. A consecutive run of `SERIES_RUN` or more on
    one example is a series and its pages are not counted, the same reading
    `_reused_prototypes` already takes: a comparison a reader makes across pages is the
    one deck this would otherwise have no way to plan, since the escape the message
    offers for it is a sentence and not a switch.

    Sharper than `_reused_prototypes`, which speaks for any repetition: this one is about
    the case where one example starts more of the deck than every other page put together.
    Only when this template's own unused examples can carry the pages that repeat, which
    is what keeps a template with two body layouts out of it -- there the repetition is
    the template's doing, and the borrow offer, which stays an offer, is the only answer.

    Measured after the fact: the run this was written against never reached it. Once the
    `ppt_template` roster stopped being dropped in transit, one plan came out on 10
    distinct prototypes over 14 pages with its most-used one starting 2, so the thing that
    produced variety was the author being told what the template ships. This check is the
    backstop for when that is not enough, not the lever.
    """
    from collections import Counter

    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.services.template.menu import menu

    if state.template is None or not outline.pages:
        return []
    entries = menu(state.template.source)
    house = {entry.number for entry in entries if entry.role}
    content = [page for page in outline.pages if not (page.prototype in house and not page.borrowed)]
    started = [page for page in content if page.prototype is not None]
    if not started:
        return []
    used = Counter((page.borrowed, page.prototype) for page in started)
    ((source, count),) = used.most_common(1)
    if count < PROTOTYPE_FLOOR or count <= len(content) * PROTOTYPE_SHARE:
        return []
    on_it = [page for page in started if (page.borrowed, page.prototype) == source]
    in_series = {number for run in _series(started) for number in run}
    # The first page keeps the prototype; the rest are the ones with somewhere to go,
    # the same division the ask makes -- less the ones inside a series, which
    # `_reused_prototypes` already reads as intentional and leaves alone. The two have
    # to agree: this one refused, as one page repeated, the consecutive run that one
    # accepts as a comparison a reader is meant to make across pages, and the refusal's
    # own offer to keep the prototype for such a series had no way to be taken.
    repeating = [page for page in on_it[1:] if page.page not in in_series]
    if not repeating:
        return []
    taken = {page.prototype for page in outline.pages if page.prototype is not None and not page.borrowed}
    examples = [entry for entry in entries if not entry.role and not entry.hidden]
    unused = [entry for entry in examples if entry.number not in taken]
    named, matched = _matched_examples(repeating, unused, examples, state)
    if not named:
        return []
    whose = f"the bundled template {source[0]}'s page {source[1]}" if source[0] else f"this template's page {source[1]}"
    listed = ", ".join(str(page.page) for page in on_it)
    return [
        Finding(
            kind="repeated_prototype",
            severity=Severity.BLOCKING,
            message=(
                f"{count} of this deck's {len(content)} content pages (pages {listed}) start on {whose}, which is "
                f"more than all its other prototypes together, and {len(unused)} of this template's "
                f"{len(examples)} content examples start no page of this deck. A deck built this way reads as one "
                "page repeated with the words changed, whatever each page says: a reader sees the arrangement "
                "before the argument. By what each of the repeating pages says it carries: "
                + "; ".join(named)
                + ". Record `prototype: N` on those pages and clone each with `clone_page(prs, prototype(tpl, N))`, "
                "then `replace_text` per line. Pages a reader is meant to compare across keep this prototype "
                f"without being counted here, so long as they run consecutively: {SERIES_RUN} or more in a row on "
                "one example is a series, and the pages in it are left alone"
            ),
            detail={
                "prototype": source[1],
                "borrowed": source[0] or None,
                "pages": [page.page for page in on_it],
                "content_pages": len(content),
                "unused_examples": [entry.number for entry in unused],
                "alternatives_for": matched,
            },
        )
    ]


def _house_pages(outline: Outline, state: Any) -> list:
    """The cover, the index and the closing page have to be the template's own.

    A requirement rather than advice, and the only one of its kind here: those three
    are the pages a reader recognises whose deck this is by, and a deck that draws its
    own cover announces itself as not theirs before a word of it is read. Blocking at
    outline time is cheap -- nothing has been built yet, and the fix is one field.

    Only for the roles the template actually has. A template with no closing page
    cannot be asked for one, and `roles` only names a page that says what it is.
    """
    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.services.template.menu import menu, roles

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
    from raven_ppt.services.assets.script_helpers import REFERENCE_DIRNAME, reference_files

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
    from raven_ppt.contracts import Finding, Severity

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
        claimed = _LAYOUT_ID_RE.findall(page.layout) + list(page.layers)
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


def _structure_enum() -> list[str]:
    """Part 1's ids, plus the empty string a template clone writes.

    An enum rather than free text because free text is what the field had when a live
    run filled it with `P01`, `P04`, `P07` -- zero-padded, so not one of them was an id,
    and the deck read down the column as if it had a range of structures in it. Built
    off the catalogue the refusal already reads, so the list the caller may pick from and
    the list it is checked against cannot come apart. Empty when the reference documents
    are not in the checkout, and an empty enum would refuse every outline, so the field
    falls back to free text there and `_invented_layouts` says the same thing it always
    did.
    """
    return _enum_of("P")


def _layer_enum() -> list[str]:
    return _enum_of("M")


def _enum_of(family: str) -> list[str]:
    known = sorted(
        (one for one in catalogue_ids() if one.startswith(family)),
        key=lambda one: int(one[1:]),
    )
    return ([""] + known) if family == "P" and known else known


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
    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.services.measure.variety import CONCENTRATED, CONCENTRATED_PAGES, MIN_PAGES, _concentration

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

    from raven_ppt.contracts import Finding, Severity

    if not outline.pages:
        return []
    reused = {
        source
        for source, count in Counter(
            (page.borrowed, page.prototype) for page in outline.pages if page.prototype is not None
        ).items()
        if count >= SAME_PROTOTYPE_IS_STRUCTURAL
    }
    furniture = [
        page.page
        for page in outline.pages
        if ((page.borrowed, page.prototype) in reused if page.prototype is not None else False)
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
    from raven_ppt.contracts import Finding, Severity

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
    none of them opened. Which page carries what is not knowable here -- `carries` is
    a sentence in the deck's own language -- so the files are named and the author
    picks; the outline no longer records a table's shape, so it cannot say which page
    will draw one.
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
    return " -- ".join(lines)


def _asks(
    errands: list,
    blocking: list,
    has_figures: bool,
    unprototyped: list[int] | None = None,
    outline: Outline | None = None,
    examples: str = "",
    reused: str = "",
) -> list[str]:
    asks: list[str] = []
    if blocking:
        asks.append("fix what is refused above and call ppt_outline again -- nothing is recorded until it clears")
        return asks
    if reused:
        asks.append(reused)
    if unprototyped:
        asks.append(
            f"pages {', '.join(str(number) for number in unprototyped)} have no template prototype. "
            + examples
            + "This deck has a template, so pick the nearest content example for each of those pages and set "
            "`prototype` on it -- `ppt_template(pages=[n])` reads one back as the code that draws it, which is "
            "how you tell whether it carries the page's information shape. Keep a page free only when none of "
            "them can after editing, and say which one you looked at and what it could not hold"
        )
    if errands:
        asks.append(
            f"get what the {len(errands)} page(s) under gather still need. Each is a gap left after the "
            "sweep of what the material cites, so this is where a search finally has a target: "
            "ppt_image_search for the page's stated need. Sweep any cited URL the intake did "
            "not reach with web_fetch first -- a picture from a page a source cites "
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
    # The tool is named because the sentence without it was the one a run acted on
    # last: it wrote no program and reached for a shell. Nothing else in the reply
    # says where a program goes or what runs it.
    asks.append(
        "then write the program, one block per page, in the order this outline sets: write_file to "
        "`deck/build/build.py`, then ppt_build runs it and returns every page it drew"
    )
    return asks
