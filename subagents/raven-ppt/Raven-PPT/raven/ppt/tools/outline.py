"""`ppt_outline`: decide what the deck argues, before drawing any of it.

The stage the route was missing. An author went from the materials straight to a
python-pptx program, so what each page said got decided while its geometry was being
typed -- and the decks were thin, eight pages of a title and three short lines, with
nothing having asked what the audience must believe by the end.

Deciding that is the author's, not this tool's. What this tool does is bind it and
check the three things about it that can be checked, each of them a whole
build-and-measure cycle earlier than it was being checked before: a number the
materials never printed, a figure that is not in the catalogue, and a page count the
brief did not agree.

And it is where gathering belongs. A page that names what it lacks becomes a search
here, at the one moment when what the deck is missing is actually known --
`ppt_prepare` has to guess it before anything knows what the pages are.
"""

from __future__ import annotations

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
from raven.ppt.services.gates import check_text, load_source_index, material_findings
from raven.ppt.services.ingest import SOURCE_INDEX_FILE
from raven.ppt.tools import _return
from raven.ppt.tools._args import ArgumentError, as_objects

# How many pages one call may plan. Past this the outline is a document rather than
# an argument, and no brief this route accepts asks for more.
MAX_PAGES = 40


class PptOutlineTool(Tool):
    name = "ppt_outline"
    description = (
        "Say what the deck argues, page by page, before you write any of it: the one thing the audience "
        "must believe at the end, and for each page the claim it makes, what carries that claim, which "
        "extracted figures it places, and the supporting points it states. Numbers in it are checked "
        "against the materials here rather than after the build, figures are checked to exist, and the "
        "page count against the brief. A page that names what it still needs comes back as something to "
        "search for -- this is the moment you know what the deck is missing. The build refuses until an "
        "outline is recorded."
    )
    timeout_seconds = 60.0

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

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
                            "figures": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "figure ids this page places, as ppt_ingest listed them",
                            },
                            "says": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "the supporting points, in the deck's language, as they will read on the "
                                    "page. Four to six for a content page, each carrying the number or the "
                                    "finding it is about rather than a heading for one; a page planned with two "
                                    "short points came out with 26 characters on it. These are checked against "
                                    "the materials"
                                ),
                            },
                            "needs": {
                                "type": "string",
                                "description": (
                                    "what this page lacks and the materials do not hold, if anything -- it "
                                    "comes back as something to go and get"
                                ),
                            },
                            "prototype": {
                                "type": "integer",
                                "description": (
                                    "with a template bound: which of its example pages this page adapts, as "
                                    "ppt_template numbered them. Decide it here, with what the page says -- "
                                    "by the time the program is being written, inventing a layout is easier "
                                    "than adapting a designed page, and that is how a template ends up used "
                                    "as a background colour. Omit only for a page the template has no page "
                                    "for, and say so in `needs`"
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

        findings = (
            _facts(deck, outline)
            + _missing_figures(outline, state)
            + _structural(outline)
            + _house_pages(outline, state)
            + _unplanned_figures(outline, state)
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
            [page.page for page in outline.pages if page.prototype is None and not page.needs.strip()]
            if state.template
            else []
        )
        return _return.done(
            blocking=blocking,
            asks=_asks(errands, blocking, bool(state.figures), unprototyped),
            **payload,
        )


def _plan(entry: dict[str, Any]) -> PagePlan:
    return PagePlan(
        page=int(entry.get("page", 0)),
        claim=str(entry.get("claim", "")).strip(),
        carries=str(entry.get("carries") or "").strip(),
        figures=tuple(str(figure) for figure in entry.get("figures") or ()),
        says=tuple(str(said) for said in entry.get("says") or () if str(said).strip()),
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


def _facts(deck: Project, outline: Outline) -> list:
    """The fact gate, run on the outline's own words.

    The same index and the same check the finished deck gets, a whole build earlier.
    Nothing ingested means no index and no gate, exactly as at build time: refusing
    every number with nothing to check it against would leave the author no move.
    """
    path = deck.ingest_dir / SOURCE_INDEX_FILE
    if not path.is_file():
        return []
    try:
        index = load_source_index(path)
    except (OSError, ValueError):
        return []
    return [finding for page in outline.pages for finding in check_text(page.text(), index, page=page.page)]


# What one page's worth of plan is, measured against what got built from it. Twelve
# pages of a live outline, each page's `says` compared to the characters that ended
# up on the finished slide:
#
#   4 says / 94-109 chars of plan  ->  200-301 chars on the page   (full)
#   3 says /  80 chars             ->  115                          (fair)
#   3 says /  57 chars             ->   33                          (empty)
#   1-2 says / 21-39 chars         ->   26-27                       (empty)
#
# So both conditions together, not either: the pages that came out empty were under
# both.
#
# Raised from 4/80 when the template's content pages stopped being prototypes. Those
# pages capped what a slide could hold -- a card slot drawn for a three-word phrase
# holds a three-word phrase -- so the plan was not the binding constraint and the floor
# only had to catch the outright empty. A page composed from scratch holds whatever it
# is given: measured across four real decks the content pages run 200-467 characters,
# and the 467 is a table with two takeaways under it that a reviewer accepted. To land
# there a page needs about five points and 140 characters of plan.
SAYS_PER_PAGE = 5
SAYS_CHARS_PER_PAGE = 140


def _thin_pages(outline: Outline, state: Any) -> list:
    """Pages whose plan is too thin to fill a slide, said while a page is cheap.

    Nothing measured this at either end. The outline checked page *count* against the
    brief and the build checked whether a page held too *much* (`density`), so a page
    planned with one bullet passed both and came back as a slide with 27 characters on
    it -- which is the complaint a reader makes first and the one no gate made.

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
        if page.figures or page.needs.strip() or (page.prototype in structural and page.prototype is not None):
            continue
        chars = sum(len(line) for line in page.says)
        if len(page.says) >= SAYS_PER_PAGE or chars >= SAYS_CHARS_PER_PAGE:
            continue
        findings.append(
            Finding(
                kind="thin_page",
                severity=Severity.WARNING,
                page=page.page,
                audience=Audience.AUTHOR,
                message=(
                    f"page {page.page} plans {len(page.says)} point(s) and {chars} characters. Pages planned "
                    f"like this came out with 26 to 57 characters on them -- a slide with a heading and one "
                    f"line, and most of the page empty. Give it more to say, merge it into its neighbour, or "
                    f"-- if it is a table, a chart or a diagram -- name the figure in `figures` or put the "
                    f"numbers it shows in `says`, because a plan that does not mention them cannot be checked "
                    f"for having them"
                ),
                detail={"says": len(page.says), "chars": chars},
            )
        )
    return findings


def _thin(deck: Project, brief: Any) -> list:
    """The material-per-page warning, repeated where the page count is still cheap.

    `ppt_brief` says it first. It is said again here because this is the last call
    before a program is written, and an author who read it as advice about the brief
    gets it once more as advice about the twelve pages now in front of them.
    """
    path = deck.ingest_dir / SOURCE_INDEX_FILE
    if not path.is_file():
        return []
    try:
        index = load_source_index(path)
    except (OSError, ValueError):
        return []
    return material_findings(index.stated_chars, brief)


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
PLANNED_EVIDENCE = 0.5


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
    # The mirror of the requirement below: a content page that names a prototype is
    # asking to be filled in, and the template's content pages are not handed out for
    # that any more. Measured on the decks that were: a slot drawn for a three-word
    # phrase set a sentence at 10.8pt, a six-card grid filled with four left a hole, and
    # a portrait picture frame could not take a landscape figure. The page is composed
    # instead, inside the house style `ppt_template` measured.
    for page in outline.pages:
        if page.prototype is None or page.prototype in named.values():
            continue
        findings.append(
            Finding(
                kind="house_page",
                severity=Severity.BLOCKING,
                page=page.page,
                audience=Audience.AUTHOR,
                message=(
                    f"page {page.page} names the template's page {page.prototype} as its prototype, and that is "
                    f"one of the template's content pages. Only its "
                    + ", ".join(f"{role} (page {number})" for role, number in named.items())
                    + " are cloned; every other page you draw yourself, inside the house style ppt_template "
                    "returns -- its layout, its title row, its type ladder, its safe area. Drop the prototype "
                    "field from this page"
                ),
                detail={"named": page.prototype, "structural": named},
            )
        )
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


def _unplanned_figures(outline: Outline, state: Any) -> list:
    """Evidence the deck has and the plan does not use."""
    from raven.ppt.contracts import Audience, Finding, Severity

    held = len(state.figures)
    if not held or not outline.pages:
        return []
    showing = sum(1 for page in outline.pages if page.figures)
    share = showing / len(outline.pages)
    if share >= PLANNED_EVIDENCE:
        return []
    return [
        Finding(
            kind="unplanned_figures",
            severity=Severity.WARNING,
            audience=Audience.AUTHOR,
            message=(
                f"{showing} of {len(outline.pages)} pages plan to show a figure, and the materials hold {held}. "
                "Name the ones that carry a page's claim in its `figures` -- an architecture a source drew is "
                "better than an architecture redrawn from its description, and a page that means to draw its "
                "own diagram or chart instead should say so in `carries`"
            ),
            detail={"pages_with_figures": showing, "pages": len(outline.pages), "figures_held": held},
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
            f"pages {', '.join(str(number) for number in unprototyped)} name no template page to adapt. This "
            "deck has a template, and most of its design is on the pages it ships rather than on its layouts "
            "-- pick a prototype for each of those pages with ppt_template, or say in `needs` why the template "
            "has nothing for it, and call ppt_outline again"
        )
    if errands:
        asks.append(
            f'get what the {len(errands)} page(s) under gather still need: web_search(kind="images") for a '
            "picture, ppt_fetch to bring it in, ppt_ingest to register it, then ppt_outline again"
        )
    if not has_figures:
        asks.append(
            "nothing visual was extracted, so every page will be prose or something you draw -- decide which "
            "pages carry a chart, a table or a diagram, and say so in `carries`"
        )
    asks.append("then write the program, one block per page, in the order this outline sets")
    return asks
