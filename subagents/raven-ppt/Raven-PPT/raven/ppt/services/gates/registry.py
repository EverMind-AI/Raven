"""One pass over a built deck, one list of findings.

The predecessor projected the same measurements three ways -- `_content_findings`
for the author, `_page_findings` for the design pass, `_render_defects` for
whoever was about to look at the render -- plus a fourth copy of the third,
inlined in the polish tool. Each returned a different shape (a list of dicts, a
dict keyed by page, another list of dicts), each wrapped its own group of checks
in one `try` so a crash in the first silenced the other two, and which audience
got what was decided by which function a call site happened to reach for. The
split was real -- content problems must not go to the design pass, which is
forbidden to change what a page says -- but it belongs on the finding, not in the
call graph.

So: one entry point runs everything, every check states its own audience and
severity, and a caller filters. Adding a check no longer means choosing which of
four functions it appears in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from raven.ppt.contracts.brief import DeckBrief
from raven.ppt.contracts.build import BuildOutcome
from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.assets.text_metrics import measurer as _font_measurer
from raven.ppt.services.gates.bands import band_findings
from raven.ppt.services.gates.brief import language_findings, page_budget_findings
from raven.ppt.services.gates.citations import citation_findings
from raven.ppt.services.gates.coverage import (
    unchecked_agreement,
    unchecked_citations,
    unchecked_facts,
    unrendered,
)
from raven.ppt.services.gates.facts import SourceIndex, fact_findings
from raven.ppt.services.gates.house_style import house_style_findings, title_row_findings
from raven.ppt.services.gates.mapping import mapping_findings
from raven.ppt.services.measure.adherence import (
    placeholder_copy,
    prototype_kept,
    template_adherence,
    template_pictures,
)
from raven.ppt.services.measure.content import (
    copy_density,
    evidence_coverage,
    flat_formulas,
    literal_escapes,
    unmarked_points,
    wide_tables,
)
from raven.ppt.services.measure.contrast import contrast_findings
from raven.ppt.services.measure.fit import overset_copy
from raven.ppt.services.measure.geometry import deck_text, slide_count
from raven.ppt.services.measure.inherited import over_layout_art
from raven.ppt.services.measure.layout import off_page_shapes, spilled_copy, wrapped_labels
from raven.ppt.services.measure.overlap import overlap_findings
from raven.ppt.services.measure.rendered import (
    card_overflows,
    cards,
    clipped_copy,
    crowded_panels,
    hairline_rules,
    orphan_lines,
    rule_strikes,
    unseparated_blocks,
    word_collisions,
)
from raven.ppt.services.measure.type_size import Span, drift_findings, rendered_spans, type_findings
from raven.ppt.services.measure.width import WidthMeasurer
from raven.ppt.services.measure.words import WordBox, words_from_pdf

# What every check is allowed to conclude, as a table rather than as a habit.
#
# Not a second place severity is decided -- each check builds its own findings --
# but the specification those checks are tested against, so a check that quietly
# starts blocking the deck fails a test instead of stalling a run. The two axes
# and why each row falls where it does:
#
# BLOCKING is reserved for claims about the source material (a number that never
# appeared in it, a figure cited as another figure), for the one design tell that
# prose demonstrably could not stop, and for a deck the pipeline cannot reason
# about at all. Everything else is a WARNING and rides along with the deck,
# because every remaining finding is satisfiable by shrinking the copy and a gate
# that refused publication until they cleared could be answered by making the
# page worse -- the oscillation of design doc D2, which ends with no deck at all.
#
# AUTHOR gets anything whose fix changes what a page says. The design pass may
# rearrange a page and never rewrite it, so density, evidence and table width go
# to the author even though they read like layout problems; handing them to the
# pass leaves it holding a problem it is forbidden to solve, which is measurably
# what happened -- the same overloaded page came back arranged into compartments
# however often it was redesigned.
DISPATCH: Mapping[str, tuple[Severity, Audience]] = {
    "fact": (Severity.BLOCKING, Audience.AUTHOR),
    # A bare acronym the materials never use. Reported, not refused: it is usually
    # ordinary vocabulary, and the checkable claims -- numbers, alphanumeric
    # identifiers -- are what "fact" above covers.
    "unfamiliar_name": (Severity.WARNING, Audience.AUTHOR),
    "citation": (Severity.BLOCKING, Audience.AUTHOR),
    "band": (Severity.BLOCKING, Audience.DESIGNER),
    "page_mapping": (Severity.BLOCKING, Audience.AUTHOR),
    "page_budget": (Severity.BLOCKING, Audience.AUTHOR),
    "language": (Severity.BLOCKING, Audience.AUTHOR),
    "density": (Severity.WARNING, Audience.AUTHOR),
    "evidence": (Severity.WARNING, Audience.AUTHOR),
    "wide_table": (Severity.WARNING, Audience.AUTHOR),
    # A page printing "48.3\\nOVIS" is not taste and not layout: the escape was escaped on
    # its way in, and the fix is one character in the author's program. Refused, with the
    # other rows about what a page says.
    "literal_escape": (Severity.BLOCKING, Audience.AUTHOR),
    # An expression written as prose. The author's, not the designer's: the fix is
    # one call in the program, and the design pass may not touch what a page says.
    "flat_formula": (Severity.WARNING, Audience.AUTHOR),
    # Parallel claims with nothing in front of them. The author's as well: the marks
    # come with the call that writes the points, and the pass may not rewrite copy.
    "unmarked_points": (Severity.WARNING, Audience.AUTHOR),
    "type_floor": (Severity.WARNING, Audience.DESIGNER),
    # One slot the deck repeats, set at five sizes because the renderer shrank each
    # box to fit what went into it. With the design pass, not the author: the sizes
    # even out by giving the boxes room, and the copy is the last resort.
    "type_drift": (Severity.WARNING, Audience.DESIGNER),
    # The one measurement that refuses. D2 said every measured layout problem
    # should only feed the loop, because hard-refusing invites the shrink-and-retry
    # oscillation text fitting already threatens. A rendered overlap is the
    # exception the user asked for after three runs delivered decks with copy
    # painted over copy: it is not taste, it is not answerable by shrinking (the
    # type floor is measured too, and shrinking trades one finding for another),
    # and the move it wants -- a wider box -- costs the page nothing.
    "word_collision": (Severity.BLOCKING, Audience.DESIGNER),
    # The quiet half of the same thing: content nothing collides with because it is
    # simply behind something. Refused for the reason `word_collision` is -- a figure
    # four fifths under a card is not answerable by making the page smaller, and a page
    # citing evidence it does not show is not a matter of taste.
    "covered_shape": (Severity.BLOCKING, Audience.DESIGNER),
    # Copy the file states and the render does not show, which is what a shape narrower
    # than its own words does: it clips instead of wrapping, and every other check
    # passes. A warning, because the render's text layer is one source of truth about
    # what a reader sees and a font that draws a glyph as blank would read the same way.
    "clipped_copy": (Severity.WARNING, Audience.DESIGNER),
    "rule_strike": (Severity.WARNING, Audience.DESIGNER),
    "card_overflow": (Severity.WARNING, Audience.DESIGNER),
    # The other side of the same rim: copy inside its panel and touching the edge. Found
    # by reading a polished deck page by page -- two note cards ended on their last
    # line's descender while every other card on that deck carried 0.20in of padding.
    "crowded_panel": (Severity.WARNING, Audience.DESIGNER),
    # A label the render broke one character short of fitting. Four of eight labels on
    # one delivered agenda page read "为什么要统 / 一"; the words do not collide, the copy
    # fits the box's height, and the type is the right size.
    "orphan_line": (Severity.WARNING, Audience.DESIGNER),
    # Two groups with no more air between them than inside them, which is the one thing
    # the design brief asks for in prose and nothing measured: "a gap that is plainly
    # wider than the gaps inside each group".
    "unseparated_blocks": (Severity.WARNING, Audience.DESIGNER),
    "wrapped_label": (Severity.WARNING, Audience.DESIGNER),
    # Copy that does not fit the box it was put in, decided by measurement rather
    # than by looking at what the renderer did with it. The render checks could only
    # report the consequence -- words on top of words -- one round later.
    "overset_copy": (Severity.WARNING, Audience.DESIGNER),
    "off_page": (Severity.WARNING, Audience.DESIGNER),
    # A shape inside the page whose copy is not: a box with wrapping off does not clip,
    # it paints straight out of itself, and two page titles of one delivered deck ran off
    # the canvas that way while every other check passed.
    "spilled_copy": (Severity.WARNING, Audience.DESIGNER),
    # Not findings about the deck at all, but about which of the rows above were
    # able to run. Warnings for the same reason the checks they stand in for
    # report nothing: an absent input is not a defect, and refusing on one would
    # be the refusal those checks correctly decline to invent.
    "unchecked_facts": (Severity.WARNING, Audience.AUTHOR),
    "unchecked_citations": (Severity.WARNING, Audience.AUTHOR),
    "unchecked_agreement": (Severity.WARNING, Audience.AUTHOR),
    "unrendered": (Severity.WARNING, Audience.AUTHOR),
    # With the language row rather than with the layout warnings: a deck in
    # somebody else's colours is not a deck the user asked for, and the fix is one
    # line at the top of the program rather than a rearranged page.
    "house_style": (Severity.BLOCKING, Audience.AUTHOR),
    # The place, where `type_drift` measures the size: the title row is the one element
    # every page of a deck shares, and a deck whose titles start at three different left
    # edges reads as three decks. Reported, because a page may earn its own treatment.
    "title_row": (Severity.WARNING, Audience.DESIGNER),
    # A placement problem the design pass fixes by moving a block, so it goes to
    # the designer and it warns: a deliberate overlay is a legitimate design, and
    # refusing on placement invites the shrink-and-retry of design doc D2.
    "over_layout_art": (Severity.WARNING, Audience.DESIGNER),
    # A deck built inside a template that did not open, index, divide or close in the
    # template's own pages. `house_style` only compares theme colours, which a program
    # passes by opening the template at all, so this was invisible: a live deck drew all
    # eight of its pages from scratch and every check came back green. The author's,
    # because which prototype a page is built on is a decision the design pass may not
    # make -- and because the content pages in between are meant to be composed rather
    # than cloned, so this no longer counts them.
    "template_adherence": (Severity.WARNING, Audience.AUTHOR),
    # And the opposite failure of the same pair: a page that cloned a prototype and
    # left its placeholder copy in place. Refused, not reported -- a page saying
    # "single-click here to add a subtitle" is finished by nobody's standard.
    "placeholder_copy": (Severity.BLOCKING, Audience.AUTHOR),
    # A template photograph still on a finished page. Reported, not refused: a
    # decorative graphic and a placeholder photograph both arrive as a PNG, and
    # deleting the first damages the page.
    "template_picture": (Severity.WARNING, Audience.AUTHOR),
    # And the mechanism that produces those: the template's page cloned for its
    # background with new text boxes laid over it. Refused for the same reason -- the
    # page shows two designs at once, and the fix is the author's program, not the
    # design pass, which may not rewrite a word.
    "template_underlay": (Severity.BLOCKING, Audience.AUTHOR),
    # A page that did not come from the prototype its own outline named. The
    # promise is the author's and so is the fix.
    "prototype_kept": (Severity.BLOCKING, Audience.AUTHOR),
    # Type a reader cannot make out: measured off the render, because what is behind a
    # text box is a layout's artwork, a photograph or a panel three shapes down, and
    # only the renderer has resolved that. Refused -- a page whose title is #1A1A1A on
    # #000000 has not been delivered, however well it is composed.
    "unreadable": (Severity.BLOCKING, Audience.AUTHOR),
    # And the band above it: legible, thin. Separated from the row above after 3:1 flat
    # refused a deck for its own template's agenda page, whose white-on-orange numerals
    # measure 2.5:1 and are what the template's designer drew.
    "thin_contrast": (Severity.WARNING, Audience.AUTHOR),
}


@dataclass(frozen=True)
class DeckUnderReview:
    """A built deck and everything that can be known about it.

    The optional fields are the reason this is a record rather than a long
    parameter list: a check with no ground truth to work from reports nothing,
    and there are several legitimate ways to arrive here short of the full set --
    no render service, so no PDF; no ingest, so no fact index; a page placing an
    image this deck never ingested.
    """

    pptx_path: Path
    pdf_path: Path | None = None
    template: Path | None = None
    prototypes: Path | None = None
    """The user's template as they handed it over, example pages included.

    Distinct from `template`, which is the prepared copy the build was pointed at --
    and prepared means the example pages were removed, so it holds no prototype to
    compare a page against. The first version of the adherence check compared against
    the prepared copy, found nothing to compare, and reported that every deck was
    fine."""
    """The template this deck was to be built inside, when the user gave one."""
    outcome: BuildOutcome | None = None
    # The outline this deck was built from, when one was recorded. It carries a
    # `prototype` per page -- a promise about which of the template's pages that page
    # would be built on -- and nothing checked it against the file until now.
    outline: Any | None = None
    source_index: SourceIndex | None = None
    figure_labels: Mapping[str, str] | None = None
    # What was agreed with the person asking for the deck. None when nobody was
    # asked, in which case the checks that need it report nothing rather than
    # inventing a budget to fail against.
    brief: DeckBrief | None = None
    words: Sequence[WordBox] | None = None
    # Rendered pages, when whoever built the deck already has them. The contrast check
    # needs pixels rather than boxes, and rendering twice for one check is what the
    # cached `rendered_words` exists to avoid.
    rendered_pages: list[Path] | None = None
    # FreeType on the bundled faces, not the font-free estimator. Measured over the
    # 498 lines of a real 20-page deck: the estimator reads narrower than the truth
    # on 80 of them and by up to 1.72x, and those are precisely the lines that
    # overflow a box nobody was told about. Falls back to the estimator only if the
    # fonts are missing.
    measurer: WidthMeasurer = field(default_factory=_font_measurer)

    type_spans: Sequence[Span] | None = None
    """The render's type, when the caller already has it. Handed over rather than read
    for the same reason `words` is: a test can state what the renderer did without a
    LibreOffice and a PyMuPDF on the box."""

    @cached_property
    def rendered_type(self) -> Sequence[Span] | None:
        """The render's own type, span by span, with the size each came out at.

        Cached beside `rendered_words` and for the same reason: two checks want it and
        reading it means parsing the whole PDF. Separate from the words because the two
        come out of different readers -- `pdftotext` gives boxes and no sizes, PyMuPDF
        gives sizes -- and a box with no PyMuPDF still gets the word checks.
        """
        if self.type_spans is not None:
            return self.type_spans
        if self.pdf_path is None:
            return None
        return rendered_spans(self.pdf_path)

    @cached_property
    def rendered_words(self) -> Sequence[WordBox] | None:
        """The render's word boxes: given, else read from the PDF, else None.

        Cached, because three checks need them and reading them means running
        `pdftotext` over the whole document. The predecessor ran it separately
        for each check, so every build extracted its deck three times.
        """
        if self.words is not None:
            return self.words
        if self.pdf_path is None:
            return None
        return words_from_pdf(self.pdf_path)


def checks() -> dict[str, Callable[[DeckUnderReview], list[Finding]]]:
    """Every check that runs against a built deck, by name.

    A dict so a caller can see what will run, run one of them alone in a test,
    and so a stage can skip one it has no input for without the registry growing
    a flag per stage.
    """
    return {
        "fact": lambda deck: _facts(deck, "fact"),
        # The same pass, filtered: one gate reads the deck's copy and answers two
        # questions of it, and a row per kind is what makes the table above a
        # specification. The pass is a few regexes over text already in memory.
        "unfamiliar_name": lambda deck: _facts(deck, "unfamiliar_name"),
        "citation": lambda deck: citation_findings(deck.pptx_path, deck.figure_labels or {}),
        "band": lambda deck: band_findings(deck.pptx_path),
        "page_mapping": lambda deck: mapping_findings(deck.outcome),
        "page_budget": lambda deck: page_budget_findings(slide_count(deck.pptx_path), deck.brief),
        "house_style": lambda deck: house_style_findings(deck.pptx_path, deck.template),
        "title_row": lambda deck: title_row_findings(deck.pptx_path, deck.prototypes, deck.outline),
        "language": lambda deck: language_findings(deck.pptx_path, deck.brief),
        "density": lambda deck: copy_density(deck.pptx_path),
        "evidence": lambda deck: evidence_coverage(deck.pptx_path, _structural(deck.outline)),
        "wide_table": lambda deck: wide_tables(deck.pptx_path),
        "flat_formula": lambda deck: flat_formulas(deck.pptx_path),
        "unmarked_points": lambda deck: unmarked_points(deck.pptx_path),
        "literal_escape": lambda deck: literal_escapes(deck.pptx_path),
        "type_floor": lambda deck: type_findings(deck.pptx_path, deck.rendered_type),
        "type_drift": lambda deck: drift_findings(deck.pptx_path, deck.rendered_type),
        "off_page": lambda deck: off_page_shapes(deck.pptx_path),
        "spilled_copy": lambda deck: spilled_copy(deck.pptx_path, deck.measurer),
        "over_layout_art": lambda deck: over_layout_art(deck.pptx_path),
        "template_adherence": lambda deck: [
            f for f in template_adherence(deck.pptx_path, deck.prototypes) if f.kind == "template_adherence"
        ],
        "placeholder_copy": lambda deck: placeholder_copy(deck.pptx_path, deck.prototypes),
        "template_picture": lambda deck: template_pictures(deck.pptx_path, deck.prototypes),
        "prototype_kept": lambda deck: prototype_kept(deck.pptx_path, deck.prototypes, deck.outline),
        "unreadable": lambda deck: [
            f
            for f in contrast_findings(deck.pptx_path, deck.pdf_path, pages=deck.rendered_pages)
            if f.kind == "unreadable"
        ],
        "thin_contrast": lambda deck: [
            f
            for f in contrast_findings(deck.pptx_path, deck.pdf_path, pages=deck.rendered_pages)
            if f.kind == "thin_contrast"
        ],
        "template_underlay": lambda deck: [
            f for f in template_adherence(deck.pptx_path, deck.prototypes) if f.kind == "template_underlay"
        ],
        "wrapped_label": lambda deck: wrapped_labels(deck.pptx_path, deck.measurer),
        "overset_copy": lambda deck: overset_copy(deck.pptx_path, deck.measurer),
        # Not findings about the deck, but about which of the checks above were
        # able to run at all.
        "unchecked_facts": unchecked_facts,
        "unchecked_citations": unchecked_citations,
        "unchecked_agreement": unchecked_agreement,
        "unrendered": unrendered,
        "word_collision": lambda deck: _rendered(deck, word_collisions),
        "covered_shape": lambda deck: overlap_findings(deck.pptx_path),
        "clipped_copy": lambda deck: _rendered(deck, lambda words: clipped_copy(deck.pptx_path, words)),
        "rule_strike": lambda deck: _rendered(deck, lambda words: rule_strikes(hairline_rules(deck.pptx_path), words)),
        "card_overflow": lambda deck: _rendered(deck, lambda words: card_overflows(cards(deck.pptx_path), words)),
        "crowded_panel": lambda deck: _rendered(deck, lambda words: crowded_panels(cards(deck.pptx_path), words)),
        "orphan_line": lambda deck: _rendered(deck, lambda words: orphan_lines(deck.pptx_path, words)),
        "unseparated_blocks": lambda deck: _rendered(deck, lambda words: unseparated_blocks(deck.pptx_path, words)),
    }


def _structural(outline: Any | None) -> list[int]:
    """The deck's pages that are the template's own, from the plan that said so.

    A cover and a closing page cannot show a figure, a table or a chart, so counting
    them among the pages that failed to is two pages of every deck's share spent on
    pages the check is not about.
    """
    pages = getattr(outline, "pages", ()) if outline is not None else ()
    return [page.page for page in pages if getattr(page, "prototype", None) is not None]


def check_deck(
    deck: DeckUnderReview,
    *,
    only: Iterable[str] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[Finding]:
    """Run every check and return what they found, in registry order.

    A check that raises is skipped rather than allowed to fail the build: a
    measurement is never worth losing a deck over, and python-pptx raises on
    shapes it does not model. Isolated per check, not per group -- the
    predecessor wrapped three checks in one `try`, so a crash in the first meant
    the other two reported a clean page.
    """
    wanted = set(only) if only is not None else None
    found: list[Finding] = []
    for name, check in checks().items():
        if wanted is not None and name not in wanted:
            continue
        try:
            found.extend(check(deck))
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            if on_error is not None:
                on_error(name, exc)
    return found


def for_audience(findings: Iterable[Finding], audience: Audience) -> list[Finding]:
    """The findings one party can act on."""
    return [finding for finding in findings if finding.audience is audience]


def by_page(findings: Iterable[Finding]) -> dict[int, list[Finding]]:
    """Page-numbered findings grouped by page; deck-wide ones are left out.

    What a per-page pass wants, and the only shape the predecessor's
    `_page_findings` produced -- now a view over the one list rather than a
    second traversal with its own idea of which checks belong in it.
    """
    grouped: dict[int, list[Finding]] = {}
    for finding in findings:
        if finding.page is not None:
            grouped.setdefault(finding.page, []).append(finding)
    return grouped


def _facts(deck: DeckUnderReview, kind: str) -> list[Finding]:
    return [f for f in fact_findings(deck_text(deck.pptx_path), deck.source_index) if f.kind == kind]


def _rendered(deck: DeckUnderReview, check: Callable[[Sequence[WordBox]], list[Finding]]) -> list[Finding]:
    """Run a check that needs the render, or nothing when there is no render.

    No PDF and no `pdftotext` are the same case: no signal. A check with no
    signal reports nothing, which is not the same as reporting that every page is
    clean, and callers must not read it as such.
    """
    words = deck.rendered_words
    return check(words) if words is not None else []
