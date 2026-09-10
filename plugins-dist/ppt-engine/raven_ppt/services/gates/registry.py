"""One pass over a built deck, one list of findings.

The predecessor projected the same measurements three ways -- `_content_findings`,
`_page_findings`, `_render_defects` -- plus a fourth copy of the third, inlined in
the polish tool. Each returned a different shape (a list of dicts, a dict keyed by
page, another list of dicts), each wrapped its own group of checks in one `try` so a
crash in the first silenced the other two, and which of the three a problem appeared
in was decided by which function a call site happened to reach for.

So: one entry point runs everything, every check states its own severity, and a
caller filters. Adding a check no longer means choosing which of four functions it
appears in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from raven_ppt.contracts.brief import DeckBrief
from raven_ppt.contracts.build import BuildOutcome
from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.contracts.masters import Bands
from raven_ppt.services.assets.text_metrics import measurer as _font_measurer
from raven_ppt.services.gates.bands import band_findings
from raven_ppt.services.gates.brief import language_findings, page_budget_findings
from raven_ppt.services.gates.citations import citation_findings
from raven_ppt.services.gates.coverage import (
    unchecked_agreement,
    unchecked_citations,
    unrendered,
)
from raven_ppt.services.gates.house_style import house_style_findings, title_row_findings
from raven_ppt.services.gates.mapping import mapping_findings
from raven_ppt.services.measure.adherence import (
    layout_photographs,
    placeholder_copy,
    prototype_kept,
    template_adherence,
    template_pictures,
    unit_marks,
)
from raven_ppt.services.measure.alignment import flush_drift
from raven_ppt.services.measure.captions import caption_findings
from raven_ppt.services.measure.content import (
    banded_tables,
    evidence_coverage,
    flat_formulas,
    listed_claims,
    literal_escapes,
    native_tables,
    wide_tables,
)
from raven_ppt.services.measure.contrast import contrast_findings
from raven_ppt.services.measure.density import (
    sparse_containers,
    thin_copy,
    unanchored_pages,
    undivided_bodies,
)
from raven_ppt.services.measure.figures import figure_findings, washed_backdrops
from raven_ppt.services.measure.fit import overset_copy
from raven_ppt.services.measure.furniture import footer_findings, grid_findings
from raven_ppt.services.measure.geometry import slide_count
from raven_ppt.services.measure.inherited import over_layout_art
from raven_ppt.services.measure.layout import off_page_shapes, spilled_copy, wrapped_labels
from raven_ppt.services.measure.overlap import overlap_findings
from raven_ppt.services.measure.quotas import equal_card_habit, repeated_layout, symmetry_habit
from raven_ppt.services.measure.rendered import (
    box_overflows,
    card_overflows,
    cards,
    clipped_copy,
    copy_boxes,
    crowded_panels,
    excessive_whitespace,
    hairline_rules,
    orphan_lines,
    rule_strikes,
    unseparated_blocks,
    word_collisions,
)
from raven_ppt.services.measure.type_size import (
    Span,
    drift_findings,
    rendered_spans,
    scale_findings,
    type_findings,
)
from raven_ppt.services.measure.variety import layout_variety
from raven_ppt.services.measure.width import WidthMeasurer
from raven_ppt.services.measure.words import WordBox, words_from_pdf
from raven_ppt.services.template.bands import bands_of

# What every check is allowed to conclude, as a table rather than as a habit.
#
# Not a second place severity is decided -- each check builds its own findings --
# but the specification those checks are tested against, so a check that quietly
# starts blocking the deck fails a test instead of stalling a run. The two axes
# and why each row falls where it does:
#
# BLOCKING is reserved for what a page credits (a figure cited as another figure),
# for what the deck was agreed to be, and for a deck the pipeline cannot reason
# about at all. It once also covered a number the materials never printed; that gate
# was deleted for firing ten times across seven runs and being wrong every time
# (design doc D3a), and nothing has replaced it. Everything else is a WARNING and rides along with the deck,
# because every remaining finding is satisfiable by shrinking the copy and a gate
# that refused publication until they cleared could be answered by making the
# page worse -- the oscillation of design doc D2, which ends with no deck at all.
#
# There was a second axis. Every row also named who was allowed to act on it,
# because a stage that could rearrange a page but not rewrite it had to be kept away
# from anything whose fix changes what a page says. That stage is gone: the author
# owns the program and can change anything in it, so every row below is the author's
# and the column said nothing. It was not free either -- filed under a heading that
# named somebody else, 25 pairs of overlapping words went unanswered across eight
# builds of one live run.
DISPATCH: Mapping[str, Severity] = {
    "citation": Severity.BLOCKING,
    # Filled colour bars carrying nothing. It refused decks until it had misfired
    # three times, each time on work that was correct, and each time the patch was
    # another exemption: eight bars on four rows read as accent strips on a page the
    # skill itself asks for ("ranking -> sorted horizontal bars"), so bar series had
    # to be detected and exempted; five full-width planes carrying formulas and a
    # takeaway row read as "a band with nothing on it" while one to six text boxes
    # sat on each, so the top-of-page requirement had to go; and a template's own
    # kicker rule, 0.04in tall and correct in the render, was refused under eight
    # titles by one eighth of a millimetre, so the hairline ceiling had to be
    # borrowed from the measurement module. A refusal has to be right the first time;
    # three exemptions in is not that, so this one reports. What it is about does not
    # change -- the strip is still the loudest tell of a generated deck.
    "band": Severity.WARNING,
    # Whether each page traces to its own block in build.py. What it protects is the
    # ability to match a render back to the code that drew it, not anything a reader
    # sees, so it reports: a deck whose pages are right and whose program is arranged
    # oddly is a deck, and refusing it buys the reader nothing.
    "page_mapping": Severity.WARNING,
    "page_budget": Severity.BLOCKING,
    "language": Severity.BLOCKING,
    "evidence": Severity.WARNING,
    "wide_table": Severity.WARNING,
    "native_table": Severity.WARNING,
    "banded_table": Severity.WARNING,
    # A caption written by looking at a figure rather than read off its source, and
    # naming something the materials never mention. Beside `citation` in what it is
    # about -- who says this picture is what the page says it is -- and a warning
    # rather than a refusal, because the name may well be printed in the pixels and
    # the answer is one sentence rather than a rebuilt page.
    "inferred_caption": Severity.WARNING,
    # A page whose outline planned a table and whose file holds none. With `citation`,
    # `page_budget` and `language` rather than with the layout warnings: it is a
    # disagreement between the plan and the file rather than a reading of a rendered
    # page, so D2's oscillation argument does not reach it -- nothing about it is
    # answered by making the page smaller. Refused, because the plan is what every
    # page after it is written against and because both answers are one edit: draw the
    # table, or call ppt_outline again for a plan that does not promise one. The twin
    # of `unplaced_figure`, which every route already refuses.
    # And the same page drawing a grid of another size. Reported, not refused: the
    # plan-time width warning tells an author to drop the columns the claim does not
    # rest on, so a refusal here would refuse the fix that warning asks for. What is
    # worth saying is that the plan and the deck now describe different tables.
    # The plan-time estimate, checked against the file that settled it: a plan wanting
    # more column width than the page carries, on a page whose drawn table is squeezed
    # or reaches past the margin. A warning with `wide_table`, which reports the same
    # shape -- two rows over one table, one refusing and one reporting, is the
    # contradiction `band` was downgraded for.
    # A page printing "48.3\\nOVIS" is not taste and not layout: the escape was escaped on
    # its way in, and the fix is one character in the author's program. Refused, with the
    # other rows about what a page says.
    "literal_escape": Severity.BLOCKING,
    # An expression written as prose. The fix is one call in the program.
    "flat_formula": Severity.WARNING,
    # Parallel claims stacked in one box, so they read as a list to be read out.
    # Reported and not refused with the other rows about a page's shape: the answer is
    # a block for each claim, which is a page rebuilt rather than a line changed.
    "listed_claims": Severity.WARNING,
    "type_floor": Severity.WARNING,
    # One slot the deck repeats, set at five sizes because the renderer shrank each
    # box to fit what went into it. The sizes even out by giving the boxes room, and
    # cutting the copy is the last resort.
    "type_drift": Severity.WARNING,
    # And the size the author picked rather than the one the renderer produced: copy set
    # over the floor, under `BODY_PT`, at a number the ramp does not have. A warning with
    # the two rows above it and for the same reason -- raising a size costs room, and a
    # refusal could be answered by cutting the copy back.
    "type_scale": Severity.WARNING,
    # The one measurement that refuses. D2 said every measured layout problem
    # should only feed the loop, because hard-refusing invites the shrink-and-retry
    # oscillation text fitting already threatens. A rendered overlap is the
    # exception the user asked for after three runs delivered decks with copy
    # painted over copy: it is not taste, it is not answerable by shrinking (the
    # type floor is measured too, and shrinking trades one finding for another),
    # and the move it wants -- a wider box -- costs the page nothing.
    "word_collision": Severity.BLOCKING,
    # The quiet half of the same thing: content nothing collides with because it is
    # simply behind something. Reported rather than refused, unlike `word_collision`,
    # because the two halves do not stand on the same evidence. Its loud half reads the
    # render and refuses what a reader can be shown not to see; this one infers from the
    # file's z-order that a later shape is opaque and covers three fifths of an earlier
    # one, and that inference has already refused work that was correct: a grouped-shape
    # coordinate bug gave five refusals on one sound contents page (D15), and over
    # twenty real decks it fires on three, one of which is a bundled template whose page
    # renders exactly as designed while the finding says its card is wholly hidden.
    # Its own message says "Nothing on this page collides" -- the render disagrees with
    # it, and the refusal was the inference winning that argument. A refusal has to be
    # right the first time; this one is not yet. The reading stays.
    "covered_shape": Severity.WARNING,
    # A container holding far less than the room it took, or with a hole between the
    # things it does hold. Reported: how full a card should be is the author's call and
    # a page can be deliberately airy, but a card whose copy stops an inch short of its
    # own footnote is a card nobody finished.
    "sparse_container": Severity.WARNING,
    # A page offering the eye nowhere to land -- no display figure and no picture with
    # any weight. Reported, because what carries a page is a judgement.
    "no_anchor": Severity.WARNING,
    # Several blocks of copy in the body with nothing drawn between them. Reported and
    # not refused: one device is enough and a page may earn its plainness, but a body
    # whose grouping is only implied by where the boxes were put is a grid the reader
    # has to infer. 104 of the 105 content pages the bundled templates ship draw one.
    "undivided_body": Severity.WARNING,
    # A page carrying less copy than its kind of page needs to say anything.
    "thin_copy": Severity.WARNING,
    # A picture stretched: the box's shape and the shape of the part of the file it
    # shows disagree. Reported rather than refused because the reading is new and the
    # crop and cover paths took three passes to read correctly.
    "figure_distortion": Severity.WARNING,
    # A frame that keeps less than half of its image: the crop, not the picture, is
    # what the reader sees. Reported, since the fix is a picture of another shape.
    "figure_crop": Severity.WARNING,
    # A picture too small for the job its size says it is doing.
    "figure_undersized": Severity.WARNING,
    # A mark that sits somewhere different on each page it appears on.
    "figure_mark_drift": Severity.WARNING,
    # A small picture parked in the title band, which is the deck's own furniture.
    "title_band_figure": Severity.WARNING,
    # A picture that is the page, washed to a share between texture and full strength:
    # type over it reads on fog. The cover that works has the photograph at full
    # strength under a plane of ink.
    "washed_backdrop": Severity.WARNING,
    # Two lines the file gives the same left edge and the render does not. The file's
    # own numbers say the author meant them flush, so the drift is a slip rather than
    # an indent -- the inset a text box carries is not visible in the geometry the
    # author wrote. Reported: it is a slip worth naming, not a page worth refusing.
    "flush_drift": Severity.WARNING,
    # The layout budgets, read off the built pages rather than off the plan that named
    # a layout: a page next to a page of the same shape, a deck that keeps reaching for
    # a row of equal cards, and a deck that divides every body into equal parts. All
    # three report -- repeating a shape can be the argument, and "varied enough" is not
    # a property a page has -- but the reading is what nobody had.
    "repeated_layout": Severity.WARNING,
    "equal_card_habit": Severity.WARNING,
    "symmetry_habit": Severity.WARNING,
    # Whether a page says which page it is. Every bundled template defines a page-number
    # placeholder on its master and puts one on no page, and python-pptx does not clone
    # a footer placeholder onto a slide -- so twelve of twelve templates and the decks
    # built from them shipped with none, and nothing measured it.
    "no_footer": Severity.WARNING,
    "unnumbered_pages": Severity.WARNING,
    # Groups of copy with nothing drawn around them. Everything on the page may pass --
    # nothing overflows, nothing collides, nothing is thin -- and a reader still cannot
    # see where one group ends, which is what a delivered deck was called out for.
    # Two column grids on one page. `flush_drift` asks whether the render kept boxes that
    # declare the same edge; this asks about edges declared 0.12in apart, which no reader
    # reads as a decision -- and where the grids' steps differ the miss grows across the
    # page.
    "grid_drift": Severity.WARNING,
    # Copy the file states and the render does not show, which is what a shape narrower
    # than its own words does: it clips instead of wrapping, and every other check
    # passes. A warning, because the render's text layer is one source of truth about
    # what a reader sees and a font that draws a glyph as blank would read the same way.
    "clipped_copy": Severity.WARNING,
    "rule_strike": Severity.WARNING,
    "card_overflow": Severity.WARNING,
    # The same escape measured against the box the copy was actually put in rather than
    # against a panel it sits over, which is the gap a live page fell through: its last
    # line rendered 0.30in under its own text box, over the template's corner ornament,
    # and all sixteen findings on that deck were about something else. A warning, with
    # `card_overflow` and `overset_copy` and against the argument that would make it
    # refuse. It is real and it is not taste -- but unlike `word_collision`, whose fix
    # is a wider box and costs the page nothing, the fix here is height taken from a
    # neighbour or a line cut, which is exactly the trade D2 says a refusal must not
    # force. And `overset_copy` predicts this same defect from font metrics and only
    # reports: one deck state, two rows, one refusing and one reporting, is the
    # contradiction `band` was downgraded for.
    "box_overflow": Severity.WARNING,
    # The other side of the same rim: copy inside its panel and touching the edge. Found
    # by reading a polished deck page by page -- two note cards ended on their last
    # line's descender while every other card on that deck carried 0.20in of padding.
    "crowded_panel": Severity.WARNING,
    # A label the render broke one character short of fitting. Four of eight labels on
    # one delivered agenda page read "为什么要统 / 一"; the words do not collide, the copy
    # fits the box's height, and the type is the right size.
    "orphan_line": Severity.WARNING,
    # Two groups with no more air between them than inside them, which is the one thing
    # the design brief asks for in prose and nothing measured: "a gap that is plainly
    # wider than the gaps inside each group".
    "unseparated_blocks": Severity.WARNING,
    "excessive_whitespace": Severity.WARNING,
    # A deck whose composed pages nearly all came out as the same arrangement of the
    # same kinds of thing. A warning and deliberately not more: "varied enough" is not
    # a property a page has, a series of pages built alike so a reader can compare them
    # is good work, and a refusal here would be a refusal of a design judgement the
    # measurement is not entitled to make. What it is entitled to is the reading, which
    # nothing else produces -- every page of the deck this was written for passed every
    # other check while four of them were the same eight lines of code. The plan's
    # `layout` column is read for the same concentration a build earlier, by
    # `tools.outline._layout_spread`, off the same thresholds; that reading is not a
    # row here because this table is what a *built* deck is held to.
    "layout_variety": Severity.WARNING,
    "wrapped_label": Severity.WARNING,
    # Copy that does not fit the box it was put in, decided by measurement rather
    # than by looking at what the renderer did with it. The render checks could only
    # report the consequence -- words on top of words -- one round later.
    "overset_copy": Severity.WARNING,
    # And the half of that reading which does not run on into room below the frame.
    # A frame anchored to the middle or the bottom of its box sets the lines it cannot
    # hold *above* the first one, so the copy leaves the page or lands on copy that is
    # already there -- the second measurement that refuses, and it passes the test D2
    # set for the first one. It is not taste: the words are somewhere no reader reads
    # them, and the render agrees -- one delivered page's four card labels each painted
    # their first line over the sentence beneath them. It is not answerable by
    # shrinking: a frame that shrinks its type to fit displaces nothing and is not
    # reported here at all, so the check never asks for the trade the type floor would
    # then refuse. And the move it wants costs the page nothing -- one anchor, or one
    # place() -- which is exactly `word_collision`'s argument, made one round earlier
    # off font metrics instead of off the render. What it deliberately does not refuse
    # is the other direction: copy running on below its frame is `box_overflow`'s
    # report, whose fix is height taken from a neighbour or a line cut, which is the
    # trade D2 says a refusal must not force. Calibrated before it was raised: over the
    # ten bundled templates' own copy in their own boxes, 42 boxes are overset and none
    # is displaced; over 25 delivered decks it fires on one, three times, on the page
    # its run was called out for.
    "displaced_copy": Severity.BLOCKING,
    "off_page": Severity.WARNING,
    # A shape inside the page whose copy is not: a box with wrapping off does not clip,
    # it paints straight out of itself, and two page titles of one delivered deck ran off
    # the canvas that way while every other check passed.
    "spilled_copy": Severity.WARNING,
    # Not findings about the deck at all, but about which of the rows above were
    # able to run. Warnings for the same reason the checks they stand in for
    # report nothing: an absent input is not a defect, and refusing on one would
    # be the refusal those checks correctly decline to invent.
    "unchecked_citations": Severity.WARNING,
    "unchecked_agreement": Severity.WARNING,
    "unrendered": Severity.WARNING,
    # With the language row rather than with the layout warnings: a deck in
    # somebody else's colours is not a deck the user asked for, and the fix is one
    # line at the top of the program rather than a rearranged page.
    "house_style": Severity.BLOCKING,
    # The place, where `type_drift` measures the size: the title row is the one element
    # every page of a deck shares, and a deck whose titles start at three different left
    # edges reads as three decks. Reported, because a page may earn its own treatment.
    "title_row": Severity.WARNING,
    # A placement problem, answered by moving a block, and it warns: a deliberate
    # overlay is a legitimate design, and refusing on placement invites the
    # shrink-and-retry of design doc D2.
    "over_layout_art": Severity.WARNING,
    # A deck built inside a template that did not open, index, divide or close in the
    # template's own pages. `house_style` only compares theme colours, which a program
    # passes by opening the template at all, so this was invisible: a live deck drew all
    # eight of its pages from scratch and every check came back green. The content
    # pages in between are meant to be composed rather than cloned, so this no longer
    # counts them.
    "template_adherence": Severity.WARNING,
    # And the opposite failure of the same pair: a page that cloned a prototype and
    # left its placeholder copy in place. Refused, not reported -- a page saying
    # "single-click here to add a subtitle" is finished by nobody's standard.
    "placeholder_copy": Severity.BLOCKING,
    # A template photograph still on a finished page. Reported, not refused: a
    # decorative graphic and a placeholder photograph both arrive as a PNG, and
    # deleting the first damages the page.
    "template_picture": Severity.WARNING,
    # The same photograph, on the layout every page inherits rather than on the page.
    "layout_picture": Severity.WARNING,
    # The marks beside a page's units, read together: still the template's, or one mark
    # on two things. Under the band `template_picture` reads as content, and reported for
    # the same reason -- which mark a unit wants is the author's to say.
    "same_mark": Severity.WARNING,
    # And the mechanism that produces those: the template's page cloned for its
    # background with new text boxes laid over it. Refused for the same reason -- the
    # page shows two designs at once, and the fix is in the author's program.
    "template_underlay": Severity.BLOCKING,
    # A page that did not come from the prototype its own outline named. Reported
    # rather than refused: the mismatch is real, but it is a mismatch between a plan
    # and a page and says nothing about the page. One run drew its own four-card
    # layout where the outline had promised the template's page 3, and the page reads
    # well -- refusing it asked for a worse page. The escape the message itself offers,
    # "or change the outline", is one line, so refusal bought nothing either.
    "prototype_kept": Severity.WARNING,
    # Type a reader cannot make out: measured off the render, because what is behind a
    # text box is a layout's artwork, a photograph or a panel three shapes down, and
    # only the renderer has resolved that. Refused -- a page whose title is #1A1A1A on
    # #000000 has not been delivered, however well it is composed. The only contrast
    # row: a warning above it for legible-but-thin type reported the template
    # designer's own accent panels far more often than a fault, and `contrast.py`
    # keeps the measurements that retired it.
    "unreadable": Severity.BLOCKING,
}


@dataclass(frozen=True)
class DeckUnderReview:
    """A built deck and everything that can be known about it.

    The optional fields are the reason this is a record rather than a long
    parameter list: a check with no ground truth to work from reports nothing,
    and there are several legitimate ways to arrive here short of the full set --
    no render service, so no PDF; no ingest, so no figure catalogue; a page placing
    an image this deck never ingested.
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
    figure_labels: Mapping[str, str] | None = None
    figure_catalogue: Mapping[str, Mapping[str, object]] | None = None
    materials: str = ""
    """Everything this deck was given to read, as one string.

    The only thing a name in a caption can be checked against: a caption asserting a
    product the materials never mention is asserting it from nowhere. Empty when
    nothing was ingested, in which case the check that needs it reports nothing
    rather than flagging every name it sees."""
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

    band_grid: Bands | None = None
    """The template's own three bands, when the caller already measured them.

    Handed over rather than derived for the same reason as the render: the checks
    that need a coordinate system must be able to state one in a test."""

    type_spans: Sequence[Span] | None = None
    """The render's type, when the caller already has it. Handed over rather than read
    for the same reason `words` is: a test can state what the renderer did without a
    LibreOffice and a PyMuPDF on the box."""

    @cached_property
    def bands(self) -> Bands | None:
        """The bands to judge against: given, else measured off the template, else None.

        None is an answer, not a gap: without the grid the checks that speak in
        shares of the body have no denominator, and they report nothing rather
        than inventing one.
        """
        if self.band_grid is not None:
            return self.band_grid
        if self.prototypes is None or not Path(self.prototypes).is_file():
            return None
        return bands_of(Path(self.prototypes), self.pdf_path)

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
        "citation": lambda deck: citation_findings(deck.pptx_path, deck.figure_labels or {}),
        "band": lambda deck: band_findings(deck.pptx_path),
        "page_mapping": lambda deck: mapping_findings(deck.outcome),
        "page_budget": lambda deck: page_budget_findings(slide_count(deck.pptx_path), deck.brief),
        "house_style": lambda deck: house_style_findings(deck.pptx_path, deck.template),
        "title_row": lambda deck: title_row_findings(deck.pptx_path, deck.prototypes, _layout_structural(deck.outline)),
        "language": lambda deck: language_findings(deck.pptx_path, deck.brief),
        "evidence": lambda deck: evidence_coverage(deck.pptx_path, _structural(deck.outline, deck.prototypes)),
        "wide_table": lambda deck: wide_tables(deck.pptx_path),
        "native_table": lambda deck: native_tables(deck.pptx_path),
        "banded_table": lambda deck: banded_tables(deck.pptx_path),
        "inferred_caption": lambda deck: caption_findings(deck.figure_catalogue, deck.materials),
        # One reading, filtered three ways, the way the adherence rows already are: the
        # three findings come off one pass over the plan and the file, and each answers
        # differently, so each needs its own row in the table above.
        "flat_formula": lambda deck: flat_formulas(deck.pptx_path),
        "listed_claims": lambda deck: listed_claims(deck.pptx_path),
        "literal_escape": lambda deck: literal_escapes(deck.pptx_path),
        "type_floor": lambda deck: type_findings(deck.pptx_path, deck.rendered_type),
        "type_drift": lambda deck: drift_findings(deck.pptx_path, deck.rendered_type),
        # `prototypes` and not `template`: the prepared copy has its example pages removed,
        # so it holds no page a box could have been cloned from and every box would read as
        # the author's. The same argument `template_adherence` is wired on.
        "type_scale": lambda deck: scale_findings(deck.pptx_path, deck.prototypes),
        "off_page": lambda deck: off_page_shapes(deck.pptx_path),
        "spilled_copy": lambda deck: spilled_copy(deck.pptx_path, deck.measurer),
        "over_layout_art": lambda deck: over_layout_art(deck.pptx_path, deck.prototypes),
        "template_adherence": lambda deck: [
            f for f in template_adherence(deck.pptx_path, deck.prototypes) if f.kind == "template_adherence"
        ],
        "placeholder_copy": lambda deck: placeholder_copy(deck.pptx_path, deck.prototypes, _borrowed(deck.outline)),
        "template_picture": lambda deck: template_pictures(deck.pptx_path, deck.prototypes, _borrowed(deck.outline)),
        "layout_picture": lambda deck: layout_photographs(deck.pptx_path, deck.prototypes),
        "same_mark": lambda deck: unit_marks(deck.pptx_path, deck.prototypes, _borrowed(deck.outline)),
        "prototype_kept": lambda deck: prototype_kept(deck.pptx_path, deck.prototypes, deck.outline),
        # `prototypes` so the refusal can name who drew the shape: "the template drew
        # it" is what a live author answered a contrast finding with, about six chevrons
        # its own program drew, and which of the two files the fix belongs in is the
        # whole of what to do next. `prototypes` and not `template` for the reason
        # `type_scale` above gives.
        "unreadable": lambda deck: contrast_findings(
            deck.pptx_path, deck.pdf_path, pages=deck.rendered_pages, prototypes=deck.prototypes
        ),
        "template_underlay": lambda deck: [
            f for f in template_adherence(deck.pptx_path, deck.prototypes) if f.kind == "template_underlay"
        ],
        # The pages the deck composed, which is every page that is not one of the
        # template's own: those are meant to be alike, and counting them is how a
        # correct deck gets reported for the four pages it was supposed to clone.
        "layout_variety": lambda deck: layout_variety(deck.pptx_path, _layout_structural(deck.outline)),
        # Not through `_rendered`: that answers [] with no render, and this check's
        # own case -- a box guessed at 0.25in -- is one the file alone catches. The
        # render is handed in as a veto where there is one.
        "wrapped_label": lambda deck: wrapped_labels(deck.pptx_path, deck.measurer, deck.rendered_words),
        # One reading, filtered two ways, the way the adherence and figure rows are:
        # a box's copy is measured once and which of the two rows it lands in is which
        # way the copy went.
        "overset_copy": lambda deck: _of_kind(overset_copy(deck.pptx_path, deck.measurer), "overset_copy"),
        "displaced_copy": lambda deck: _of_kind(overset_copy(deck.pptx_path, deck.measurer), "displaced_copy"),
        # Not findings about the deck, but about which of the checks above were
        # able to run at all.
        "unchecked_citations": unchecked_citations,
        "unchecked_agreement": unchecked_agreement,
        "unrendered": unrendered,
        "word_collision": lambda deck: _rendered(deck, word_collisions),
        "covered_shape": lambda deck: overlap_findings(deck.pptx_path),
        "sparse_container": lambda deck: sparse_containers(
            deck.pptx_path, deck.rendered_words, _structural(deck.outline, deck.prototypes)
        ),
        "no_anchor": lambda deck: unanchored_pages(
            deck.pptx_path, deck.rendered_type, deck.bands, _structural(deck.outline, deck.prototypes)
        ),
        "undivided_body": lambda deck: undivided_bodies(
            deck.pptx_path, deck.bands, _structural(deck.outline, deck.prototypes)
        ),
        "thin_copy": lambda deck: thin_copy(deck.pptx_path, deck.outline, deck.prototypes),
        "figure_distortion": lambda deck: _of_kind(figure_findings(deck.pptx_path, deck.bands), "figure_distortion"),
        "figure_crop": lambda deck: _of_kind(figure_findings(deck.pptx_path, deck.bands), "figure_crop"),
        "figure_undersized": lambda deck: _of_kind(figure_findings(deck.pptx_path, deck.bands), "figure_undersized"),
        "figure_mark_drift": lambda deck: _of_kind(figure_findings(deck.pptx_path, deck.bands), "figure_mark_drift"),
        "title_band_figure": lambda deck: _of_kind(figure_findings(deck.pptx_path, deck.bands), "title_band_figure"),
        "washed_backdrop": lambda deck: washed_backdrops(deck.pptx_path),
        # Only the flush reading is dispatched. Its sibling, which compares the insides
        # of a row of cards, found nothing true and nothing false over five real decks:
        # it has unit tests and no sample, and a check with no sample is a check whose
        # first report will be its first test.
        "flush_drift": lambda deck: _rendered(deck, lambda words: flush_drift(deck.pptx_path, words)),
        "no_footer": lambda deck: _of_kind(
            footer_findings(deck.pptx_path, _layout_structural(deck.outline), deck.prototypes), "no_footer"
        ),
        "unnumbered_pages": lambda deck: _of_kind(
            footer_findings(deck.pptx_path, _layout_structural(deck.outline), deck.prototypes), "unnumbered_pages"
        ),
        "grid_drift": lambda deck: grid_findings(deck.pptx_path, _layout_structural(deck.outline)),
        "repeated_layout": lambda deck: repeated_layout(deck.pptx_path, _layout_structural(deck.outline)),
        "equal_card_habit": lambda deck: equal_card_habit(deck.pptx_path, _layout_structural(deck.outline)),
        "symmetry_habit": lambda deck: symmetry_habit(deck.pptx_path, _layout_structural(deck.outline)),
        "clipped_copy": lambda deck: _rendered(deck, lambda words: clipped_copy(deck.pptx_path, words)),
        "rule_strike": lambda deck: _rendered(deck, lambda words: rule_strikes(hairline_rules(deck.pptx_path), words)),
        "card_overflow": lambda deck: _rendered(
            deck,
            lambda words: card_overflows(cards(deck.pptx_path), words, copy_by_page=copy_boxes(deck.pptx_path)),
        ),
        "box_overflow": lambda deck: _rendered(deck, lambda words: box_overflows(deck.pptx_path, words)),
        "crowded_panel": lambda deck: _rendered(deck, lambda words: crowded_panels(cards(deck.pptx_path), words)),
        "orphan_line": lambda deck: _rendered(deck, lambda words: orphan_lines(deck.pptx_path, words)),
        "unseparated_blocks": lambda deck: _rendered(deck, lambda words: unseparated_blocks(deck.pptx_path, words)),
        "excessive_whitespace": lambda deck: _rendered(
            deck,
            lambda words: excessive_whitespace(deck.pptx_path, words, _layout_structural(deck.outline)),
        ),
    }


def _structural(outline: Any | None, prototypes: Path | None = None) -> list[int]:
    """The deck's pages that are the template's own furniture.

    A cover and a closing page cannot show a figure, a table or a chart, so counting
    them among the pages that failed to is two pages of every deck's share spent on
    pages the check is not about.

    Naming a prototype does not make a page furniture: since content pages default to
    building on one, `prototype is not None` came to mean every page, and this check
    stopped judging any of them. What the prototype does say is which template page a
    deck page clones, and that page's own role is the answer.
    """
    pages = getattr(outline, "pages", ()) if outline is not None else ()
    named: dict[int, str] = {}
    if prototypes is not None and Path(prototypes).is_file():
        from raven_ppt.services.template.menu import menu

        named = {entry.number: entry.role for entry in menu(Path(prototypes))}
    furniture = [
        page.page
        for page in pages
        # A borrowed prototype is numbered in another file, so the bound template's
        # roles say nothing about it -- and a borrowed page is a content page by
        # construction, since only content pages are offered for borrowing.
        if not getattr(page, "borrowed", "") and named.get(getattr(page, "prototype", None) or 0, "")
    ]
    return sorted(set(furniture) | set(_layout_structural(outline)))


def _borrowed(outline: Any | None) -> list[Path]:
    """The bundled templates this deck borrowed pages from, as files, in plan order."""
    from raven_ppt.services.template.defaults import bundled_path

    found: list[Path] = []
    for page in getattr(outline, "pages", ()) if outline is not None else ():
        path = bundled_path(getattr(page, "borrowed", ""))
        if path is not None and path not in found:
            found.append(path)
    return found


def _layout_structural(outline: Any | None) -> list[int]:
    pages = getattr(outline, "pages", ()) if outline is not None else ()
    markers = (
        "封面",
        "目录",
        "章节",
        "分隔",
        "收尾",
        "封底",
        # The words a Chinese deck actually ends on. Without them the fallback --
        # now the only path when no template is bound -- misses every closing page.
        "结束",
        "尾页",
        "谢谢",
        "cover",
        "agenda",
        "contents",
        "section divider",
        "closing",
    )
    structural: list[int] = []
    for page in pages:
        # What this page carries, and its section only where it carries nothing. A
        # section names the movement a run of pages belongs to -- the contract's own
        # example list includes "closing and disclaimers" -- so reading the two together
        # made every page of such a movement structural: a chart page and a table page
        # inside it were exempted from checks that exist for content pages.
        carries = str(getattr(page, "carries", "") or "").casefold()
        role = carries or str(getattr(page, "section", "") or "").casefold()
        if any(marker in role for marker in markers):
            structural.append(page.page)
    return structural


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

    One cause is reported once. See `_one_per_cause` -- a check that fires on
    nineteen pages for one shared reason arrived as nineteen findings, and the
    model read them as nineteen problems.
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
    return _one_per_cause(found)


def _one_per_cause(findings: list[Finding]) -> list[Finding]:
    """Findings that repeat one cause across pages, collapsed to one that names them.

    A live 19-page run came back with the same title-row warning on every page: one
    shared cause, in the setup every page is drawn from, and nineteen findings saying
    so. The model spent a round on each, because nineteen concrete per-page problems
    read as nineteen decisions. This is the same fold `build`'s root-cause note makes
    for a page wrong at the root, applied across pages rather than down one.

    What counts as one cause is read off the findings rather than declared here: same
    kind, same severity, and the same message word for word. A message
    that names what is wrong with the page it is on -- a number that page states, a
    placeholder it left in -- differs per page and stays per page, which is why there
    is no list of kinds to keep in step with the registry. More than one page is what
    makes a cause repeated; there is no count to tune.

    The collapsed finding is deck-wide, because that is what it is: the pages it names
    are in `on_pages` and in the message, and its severity is the one it had, so an
    aggregated blocking finding still blocks. That also takes it out of `by_page`,
    which is the intended half of the trade -- a cause shared by nineteen pages is not
    answered by a pass that sees one page at a time.
    """
    grouped: dict[tuple[str, Severity, str], list[Finding]] = {}
    # A finding, or the cause standing in for the group that starts here, so the run
    # comes back in registry order with each group where its first member was.
    kept: list[Finding | tuple[str, Severity, str]] = []
    for finding in findings:
        if finding.page is None:
            kept.append(finding)
            continue
        cause = (finding.kind, finding.severity, finding.message)
        if cause not in grouped:
            grouped[cause] = []
            kept.append(cause)
        grouped[cause].append(finding)
    if all(len({f.page for f in same}) < 2 for same in grouped.values()):
        return findings
    out: list[Finding] = []
    for entry in kept:
        if isinstance(entry, Finding):
            out.append(entry)
            continue
        same = grouped[entry]
        pages = sorted({f.page for f in same if f.page is not None})
        if len(pages) < 2:
            out.extend(same)
            continue
        first = same[0]
        named = ", ".join(str(page) for page in pages)
        out.append(
            Finding(
                kind=first.kind,
                severity=first.severity,
                page=None,
                message=f"pages {named} all report this, so it is one cause and not {len(pages)}: {first.message}",
                detail={**dict(first.detail), "on_pages": pages},
            )
        )
    return out


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


def _of_kind(findings: list[Finding], kind: str) -> list[Finding]:
    """One reading out of a measurement that answers several at once.

    The four figure readings come from one pass over the pictures, and the registry
    hands each out under its own name so a caller can run one alone. The pass is cheap
    beside opening the deck, which every check does anyway.
    """
    return [finding for finding in findings if finding.kind == kind]


def _rendered(deck: DeckUnderReview, check: Callable[[Sequence[WordBox]], list[Finding]]) -> list[Finding]:
    """Run a check that needs the render, or nothing when there is no render.

    No PDF and no `pdftotext` are the same case: no signal. A check with no
    signal reports nothing, which is not the same as reporting that every page is
    clean, and callers must not read it as such.
    """
    words = deck.rendered_words
    return check(words) if words is not None else []
