"""Build the deck, measure it, deliver it.

The order is the argument. Build, then measure the *file* rather than the
submission -- what an audience sees includes whatever a helper composed on the
way. Then publish, or refuse.

There was a stage between the measuring and the publishing: a design pass that
rendered each page onto an empty context and rewrote the block that drew it. It
is gone, and the iteration it stood for now happens where the author is. Every
build hands back the renders, so the author looks at the page it just wrote,
edits the program and builds again -- which is the same loop with one fewer
actor in it, and the actor removed was the one that could see the page but not
change what it said.

One refusal is not about the deck's content at all: a page whose code has never
been rendered back to the author. Every tool description here says to look at every
page and none of it was enforceable -- publication happens below, before the renders
are even fetched, so `slides=[1]` on an eighteen-page deck shipped seventeen pages
nobody had seen. What is recorded is that the tool handed a page over, which is the
closest thing to attention that exists, and it is keyed on the code that drew the
page so that editing one makes it unseen again. Skipped when the machine cannot
render at all: refusing a deck for a missing LibreOffice would be refusing it for the
machine rather than for itself.

Warnings ride along at every step and never stop the deck. Type size, overflow
and overlap are all satisfiable by shrinking the copy, so a gate that refused
publication until they cleared could be answered by making the page worse -- and
the loop then oscillates instead of converging, ending with no deck at all. They
are reported instead, all of them to the author, who owns the program and can
answer any of them.

One thing here is neither a finding nor a warning. A twenty-page deck is written
in one program and reviewed three pages at a time, and nothing told page 15 what
page 3 decided: with a template bound the author is handed the template's measured
house and held to it, and with no template there was nothing at all. So the built
deck is measured against itself and the answer travels back as a note -- the box
its pages put a title in, how many of them agree, the sizes its body copy comes
out at. Not a finding, because the deck it would fire on is the deck that has a
divider, a hero number and a closing in it, and those are meant to look different;
what is reported is the count, and the author reads it and decides.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from raven_ppt.backends.script import blocks_rejection, page_blocks, script_path
from raven_ppt.contracts import (
    BuildOutcome,
    Finding,
    Profile,
    Project,
    Severity,
    StageResult,
    load_outline,
    outline_path,
)
from raven_ppt.services import seen
from raven_ppt.services.measure.geometry import iter_shapes, open_deck, shows_picture
from raven_ppt.services.measure.type_size import census, rendered_spans
from raven_ppt.services.publish import PublishRefusedError, publish, stage
from raven_ppt.services.template import house_style, prepared_path

# Default for `BuildStage.views_per_call`, which is how many page renders one reply
# carries. The effective number is the stage's field, set from `tools.ppt.viewsPerCall`,
# and `ppt_build` reads it off the stage rather than keeping a constant of its own.
# That is the whole point of one number: the unseen record is written from the pages
# the stage decided to show, so a `slides` cap that disagreed either forbids pages the
# reply would have carried or lets a call name pages the reply silently drops.
# Three is a compromise between two measured failures. Twelve renders in one reply read
# as a batch to skim -- a run voiced one problem while several stood -- and one render
# per reply makes a nineteen-page deck nineteen builds to look at once, which is most of
# why a measured run spent 147 iterations.
BATCH_VIEWS = 3

# A plurality of pages is not a majority of them, and the difference is where this
# measurement goes wrong. `_rows` answers for every page, so pages with no title in
# the band still vote and their answers cluster: on one deck three of six named a
# right-hand panel, on the same deck four of nine did (a 4-4 tie broken by insertion
# order), and on another two of five named a 16.5pt line. Every one of those is at or
# under half. Where the row really is the deck's, sixteen of nineteen said so. So the
# record names a row when most of the content pages put one there and says they have
# not settled on one when they have not -- a relation rather than a count, true at any
# deck length, and a tie falls out of it for free.

# How many of the deck's body sizes the record lists before it stops counting them out.
HOUSE_SIZES = 3


@dataclass
class BuildStage:
    """One call: everything between a submitted program and a delivered deck."""

    backend: Any
    # Awaited: measuring means rendering the deck, which is slow and blocking.
    measure: Callable[..., Any]
    profile: Profile
    destination: Callable[[Project], Path] = field(default=lambda p: p.exports_dir / "deck.pptx")
    # One field rather than a constant on each side of the call, because those are what
    # drifted: `ppt_build` bounds `slides` by this and the unseen record is written from
    # what it selects, so the two cannot be allowed to hold different numbers.
    views_per_call: int = BATCH_VIEWS

    async def run(
        self,
        project: Project,
        script: str | None = None,
        *,
        slides: Sequence[int] | None = None,
        page_from: int = 1,
        draft: bool = False,
    ) -> StageResult:
        outcome = await self.backend(project, script)
        if not outcome.ok:
            return StageResult(ok=False, data={"outcome": outcome}, note=outcome.note)

        findings = list(await self.measure(project, outcome.pptx_path, outcome))
        findings.extend(_mapping_findings(project, outcome))
        # After measuring, because measuring is what renders the deck and the record
        # reads that render. Before either exit, because a draft is the state the
        # record is worth most in: half the program is written and the rest of it is
        # about to be.
        outcome = _with_deck_house(project, outcome)
        if draft:
            # A program still being written is an accepted intermediate state, and the
            # previous engine learned this the same way: requiring a whole deck in one
            # submission meant "tens of thousands of tokens that took minutes and that
            # the transport truncated". Holding a three-page draft to a twelve-page
            # brief pushes the author straight back into that one giant call, so the
            # length and what was agreed are not asked of a draft. Everything else is
            # measured, and nothing is published.
            findings = [finding for finding in findings if finding.kind not in _DRAFT_EXEMPT]
            showing = _showing(outcome.pages, slides, page_from, self.views_per_call)
            # A draft render is a page put in front of the author, so it counts as
            # seen. See `_record_shown` for what recording it only on publication
            # cost one measured run.
            _record_shown(project, outcome, showing, findings)
            data: dict[str, Any] = {"outcome": outcome, "findings": findings, "showing": showing, "draft": True}
            return StageResult(ok=True, findings=tuple(findings), data=data, note="draft: not published")

        showing = _showing(outcome.pages, slides, page_from, self.views_per_call)
        findings.extend(_unplaced_figure_findings(project, outcome))
        _record_shown(project, outcome, showing, findings)
        findings.extend(_unseen_findings(project, outcome))

        blocking = self._blocking(findings)
        data: dict[str, Any] = {"outcome": outcome, "findings": findings, "showing": showing}
        if blocking:
            return StageResult(ok=False, findings=tuple(findings), data=data)

        try:
            staged = stage(project, outcome.pptx_path, pages=outcome.pages)
            delivered = publish(
                project,
                staged,
                self.destination(project),
                findings=findings,
                blocking_kinds=self.profile.blocking_kinds,
            )
        except PublishRefusedError as exc:
            return StageResult(ok=False, findings=tuple(findings), data=data, note=str(exc))
        data["pptx_path"] = str(delivered)
        return StageResult(ok=True, findings=tuple(findings), data=data)

    def _blocking(self, findings: Sequence[Finding]) -> list[Finding]:
        kinds = self.profile.blocking_kinds
        return [f for f in findings if f.severity is Severity.BLOCKING or f.kind in kinds]


# What a draft is not held to: the length the brief agreed, and the pages the
# outline mapped. Both are statements about a finished deck, and a draft is not one.
_DRAFT_EXEMPT = frozenset({"page_budget", "page_mapping", "unseen_page"})


def _with_deck_house(project: Project, outcome: BuildOutcome) -> BuildOutcome:
    """The same outcome carrying a record of what this deck's own pages settled on.

    It rides on `note` because that field is already what the harness says to the
    author about a build it did on their behalf, and `ppt_build` puts it in the reply
    beside the findings. Nothing else the stage returns reaches the author as prose:
    `StageResult.note` is read only when publication was refused.
    """
    said = _deck_house(project, outcome)
    if not said:
        return outcome
    return replace(outcome, note=f"{outcome.note}\n{said}" if outcome.note else said)


def _deck_house(project: Project, outcome: BuildOutcome) -> str:
    """What this deck's pages agree on, in one paragraph, or nothing.

    Nothing when a template is bound. The house is then the template's, `ppt_template`
    hands the author its measured brief, and each page that drifts from its title row
    already gets a `title_row` finding naming both boxes -- a deck-level record of the
    deck's own house would be a third telling of that, and the one telling that reads
    as permission to keep the drift.

    The title row is named only when most of the content pages put one in the same
    place. The count is always in the sentence, so an agreement of 16 of 19 is a fact
    the author can weigh -- but a box named as the deck's own when half the pages say
    otherwise is an instruction, and at or under half the box was measurably wrong.
    Below that the record says so, which is still worth saying: a deck whose pages have
    not settled on a title row is a deck to look at.
    """
    if outcome.pptx_path is None or prepared_path(project).is_file():
        return ""
    try:
        pdf = _render_of(project, outcome.pptx_path)
        house = house_style(outcome.pptx_path, None, pdf)
        if house is None or house.title is None:
            return ""
        content = set(house.content_pages)
        agreed = house.title.pages
        if agreed * 2 <= len(content):
            facts = [
                f"{agreed} of them put a title row in the same place and the rest do not, so these pages "
                "have not settled on one"
            ]
        else:
            facts = [f"the title row {agreed} of them agree on is {house.title.line()}"]
        face = house.faces.get("text")
        if face and face != house.title.face:
            facts.append(f"most of the copy is set in {face} rather than in the title's face")
        sizes = _body_sizes(outcome.pptx_path, pdf, content)
        if sizes:
            facts.append("body copy comes out at " + sizes)
        return (
            f"this deck's own house, measured off the {len(content)} of its {outcome.pages} pages that read as "
            "content -- a cover, a contents list, a divider and a closing are left out, because those are meant "
            f"to look different. Of the rest: {'; '.join(facts)}. Nothing here is a finding and nothing refuses "
            "the deck -- it is what the pages already written settled on, so a page written after them can match "
            "it or differ from it on purpose."
        )
    except Exception:  # noqa: BLE001 -- a record is worth less than the build carrying it
        return ""


def _body_sizes(pptx: Path, pdf: Path | None, content: set[int]) -> str:
    """The sizes this deck's body copy comes out at, by how many pages sit at each.

    Measured rather than taken from `House.scale`, which cannot answer it: that ladder
    reads `body`, `secondary` and `caption` off the slide master, and a deck opened
    with `Presentation()` inherits Office's own master -- so `scale` reported 32 / 28 /
    24pt for three different real decks whose copy runs at 16 to 21. Only its `title`
    is measured off the pages. `census` measures every page, so the count here is a
    page count and not a vote that can pick the wrong winner.
    """
    spans = rendered_spans(pdf) if pdf is not None else None
    pages: dict[float, int] = {}
    for page in census(pptx, spans):
        if page.page in content and page.body_pt is not None:
            pages[page.body_pt] = pages.get(page.body_pt, 0) + 1
    if not pages:
        return ""
    ranked = sorted(pages.items(), key=lambda item: (-item[1], -item[0]))
    # Only the first entry spells out what the number counts; after that the reading is
    # established and "18pt on 8, 21pt on 3" is the shorter way to say the same thing.
    first, rest = ranked[0], ranked[1:HOUSE_SIZES]
    said = f"{first[0]:g}pt on {first[1]} page" + ("s" if first[1] != 1 else "")
    said += "".join(f", {size:g}pt on {count}" for size, count in rest)
    left = sum(count for _size, count in ranked[HOUSE_SIZES:])
    return said + (f", and {left} more at other sizes" if left else "")


def _render_of(project: Project, pptx: Path) -> Path | None:
    """The PDF the measuring pass already made of this deck, when it made one.

    Derived rather than passed: `measure` is injected, and the one implementation that
    renders (`stages/_measure.py`) hands the deck to `DeckViews.pdf(pptx, review_dir)`,
    which writes `<out_dir>/<stem>.pdf` -- the contract stated on
    `services/render/office.to_pdf`. Asking for it a second time would run LibreOffice
    again, which is seconds rather than the tens of milliseconds reading the file costs.
    The mtime check is what keeps a previous build's render from being read as this
    one's; without a render the sizes come off the file, which for a program that writes
    every size itself is the same answer.
    """
    pdf = project.review_dir / f"{pptx.stem}.pdf"
    try:
        return pdf if pdf.is_file() and pdf.stat().st_mtime >= pptx.stat().st_mtime else None
    except OSError:
        return None


def _showing(pages: int, slides: Sequence[int] | None, page_from: int, views: int = BATCH_VIEWS) -> list[int]:
    """The pages this call will render back: the ones asked for, or the next batch.

    `views` is passed rather than read off the module so one build's budget is one
    value: `ppt_build` publishes the same number as its `slides` cap, and the unseen
    record below is written from what this returns.
    """
    if slides:
        return sorted(set(slides))[:views]
    first = max(1, min(page_from, pages or 1))
    return list(range(first, min(pages, first + views - 1) + 1))


def _unplaced_figure_findings(project: Project, outcome: BuildOutcome) -> list[Finding]:
    """Pages the plan gave a figure to, that were built without one.

    The gap this closes was found by looking: a run planned a logo on page 1 and a
    repository card on page 6, fetched both, and built twenty pages carrying no
    picture at all. Nothing said so. `ppt_outline` checks that a figure the plan
    names is in the catalogue, which is a check on the plan; from there to the
    deck the figure was on nobody's list.

    Both sides are files, which is why this is measured rather than judged: the
    outline says which pages carry a figure, and a built page either shows a
    picture or does not. What it deliberately does not check is *which* picture --
    an author that crops, recolours or substitutes one has still placed a figure,
    and the finding would then be about identity rather than presence.

    Warning by severity and refused by the route, which is what a profile's
    `blocking_kinds` is for. A deck that drops the evidence it planned is not a
    deck that is nearly right, and nothing about it is fixed by shrinking or
    rewrapping something -- and it cannot deadlock, because the plan only ever
    names figures the catalogue holds and answering it is one line either way.
    """
    outline = load_outline(outline_path(project))
    if outline is None or outcome.pptx_path is None:
        return []
    planned = {page.page: list(page.figures) for page in outline.pages if page.figures}
    if not planned:
        return []
    try:
        deck = open_deck(outcome.pptx_path)
    except Exception:
        return []
    findings = []
    for number, slide in enumerate(deck.slides, 1):
        wanted = planned.get(number)
        if not wanted:
            continue
        if any(shows_picture(shape) for shape in iter_shapes(slide.shapes)):
            continue
        listed = ", ".join(wanted)
        findings.append(
            Finding(
                kind="unplaced_figure",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"the plan gives this page {listed} and the page was built without a picture on it. "
                    "Place it (`add_picture`, or `picture_fit` to scale it into a box whole), or call "
                    "ppt_outline again for a plan that does not promise it -- a figure gathered and never "
                    "placed is a page arguing from a description of evidence rather than the evidence"
                ),
                detail={"figures": wanted},
            )
        )
    return findings


def _record_shown(project: Project, outcome: BuildOutcome, showing: Sequence[int], measured: Sequence[Finding]) -> None:
    """Remember that these pages were rendered back, as their code stood.

    Called from the draft path as well as the publishing one, and that is the whole
    point: a draft render is a page put in front of the author, so it is what the
    record is about. Recording only on publication made the two states disagree --
    a measured run walked its nineteen pages three times as drafts, was told on the
    first real build that eighteen of them had never been seen, and spent the rest
    of its budget walking them again one non-draft build at a time. The pages had
    been looked at; only the record disagreed.

    A render that did not happen is still not a page seen: the `unrendered` guard
    stays, because the fingerprint would otherwise say the author was shown code
    that produced no picture.
    """
    if any(finding.kind == "unrendered" for finding in measured):
        return
    try:
        script = script_path(project).read_text(encoding="utf-8")
    except OSError:
        return
    blocks = seen.blocks_of(script, outcome.sources)
    if not blocks:
        return
    seen.record(project, {page: blocks[page] for page in showing if page in blocks})


def _unseen_findings(project: Project, outcome: BuildOutcome) -> list[Finding]:
    """Refuse a deck carrying a page whose current code nobody has been shown.

    Read here rather than in the tool because this is where publication happens: a
    check the tool ran afterwards would be a check on a file already delivered. The
    recording half is `_record_shown`, which both paths call.
    """
    try:
        script = script_path(project).read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = seen.blocks_of(script, outcome.sources)
    if not blocks:
        return []
    absent = seen.unseen(project, blocks)
    if not absent:
        return []
    listed = ", ".join(str(page) for page in absent[:12]) + ("…" if len(absent) > 12 else "")
    return [
        Finding(
            kind="unseen_page",
            severity=Severity.BLOCKING,
            message=(
                f"{len(absent)} page(s) have never been rendered back to you since the code that draws them "
                f"was written: {listed}. Run ppt_build again -- omit `slides` and pass `page_from` to walk the "
                "rest -- and look at them. A page you have not looked at is a page you have not checked, and "
                "this is the one finding you cannot answer by editing the deck"
            ),
            detail={"pages": list(absent)},
        )
    ]


def _mapping_findings(project: Project, outcome: BuildOutcome) -> list[Finding]:
    """Whether this deck's pages can be told apart in the program that drew them.

    Blocking, and it is the one blocking finding that is not about the deck's
    content: without a page-to-code mapping a build failure cannot be attributed
    and a render cannot be matched to the code that drew it -- which is the whole
    of how an author reviews a page. A pipeline that cannot reason about a page at
    all should say so rather than proceed and guess.

    The message carries a skeleton rather than a complaint. Refused without one,
    an author rewrote the same shim three times and then left the tool.
    """
    if outcome.sources:
        return []
    try:
        lines = script_path(project).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return []
    blocks = page_blocks(lines)
    reason = blocks_rejection(lines, blocks, outcome.pages, "comments") if blocks else None
    if blocks and not reason:
        return []
    return [
        Finding(
            kind="unmapped_page",
            severity=Severity.BLOCKING,
            message=(
                (reason or "build.py has no per-page blocks, so no render can be matched to the code that drew it")
                + ". Draw the pages in build.py itself, one block of it per page:\n"
                "    def new_slide():\n"
                "        return prs.slides.add_slide(prs.slide_layouts[6])\n"
                "\n"
                "    # SLIDE 1\n"
                "    sl = new_slide()\n"
                "    title(sl, 'Unified video segmentation')\n"
                "\n"
                "    # SLIDE 2\n"
                "    sl = new_slide()\n"
                "    ..."
            ),
        )
    ]
