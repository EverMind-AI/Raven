"""Which of the 43 checks a build is worth running, and when rebuilding has stopped paying.

Two failures, opposite in shape, argue for one table.

A reading that arrives at the finish line cannot be acted on: one 18-page run read
every page inside the build that delivered, reported 45 problems on 18 pages, and the
deck was published 38 seconds later, because answering 45 findings on a finished deck
means redoing every page (design doc D21). And a reading that never stops costs the
attention it was meant to buy: one live run carried a single `band` warning from 07:14
to 07:39 across nine builds and nineteen minutes of pure build time, still rebuilding
pages for it on iteration 36, and the finding was not answerable at all -- it was the
template's own device tripping a gate that had already been downgraded for misreading
exactly that (D17). D17 predicted the bill ("a known misfire is still noise, and noise
takes the attention the author should be spending on the page") and left no ceiling.

So: `TIERS` says what evidence each check consumes, `checks_for` says which ones this
particular rebuild can have moved, `converged` says when the loop has stopped answering
anything, and `NOISE_FLOOR` collects the differences already judged too small to report.

Nothing here decides severity. `registry.DISPATCH` is the only place that does, and a
check dropped from a batch is a check not run, never a check overruled: `checks_for`
returns every blocking row on every build that has the evidence for it, and returns all
43 for the build that delivers, so fail-closed publication (D3) is untouched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from raven_ppt.contracts.findings import Severity


class Tier(Enum):
    """What a check has to be handed before it can answer.

    The tiers nest in cost, which is why they are the axis: DECLARED needs the built
    file, RENDERED needs a PDF on top of it, RASTER needs the pixels on top of that.
    COPY is DECLARED's sibling rather than its successor -- it reads the same file, but
    for what the page says rather than for where its boxes are, so an edit that moves
    only geometry cannot change it and vice versa. COVERAGE stands outside all four: it
    reports which of them could run.
    """

    DECLARED = "declared"
    COPY = "copy"
    RENDERED = "rendered"
    RASTER = "raster"
    COVERAGE = "coverage"


# Every row of `registry.DISPATCH`, by the evidence it consumes. Asserted against that
# table in `tests/ppt/test_gates_grading.py`, so a check added there and not here fails
# a test rather than falling silently into whichever batch happens to run.
TIERS: Mapping[str, Tier] = {
    # -- DECLARED: the built file's geometry, fills and z-order, plus the build's own
    # page-to-code map. No render, and no judgement about what the words say.
    # A filled bar and whether anything sits on it: boxes and fills only, so rewriting a
    # sentence can neither create a band nor clear one.
    "band": Tier.DECLARED,
    # z-order plus area, read off the file -- `measure/overlap.py` says order is what
    # makes it decidable, and order is stated, never rendered.
    "covered_shape": Tier.DECLARED,
    # How much of a container its content uses, and where the hole is. Off the render
    # when there is one -- a word's box is tighter than the frame it was set in -- but
    # it answers off the file too, so the cheapest tier that can run it owns it.
    "sparse_container": Tier.DECLARED,
    # Whether anything on the page is big enough to land on. The size that decides it is
    # the rendered one, so it belongs with the readings that need a render.
    "no_anchor": Tier.RENDERED,
    # Whether anything is drawn between the page's blocks of copy. Fills, strokes and
    # boxes, all of them stated, so the cheapest tier owns it.
    "undivided_body": Tier.DECLARED,
    # How much copy the page carries, read off the file's own runs against what its kind
    # of page needs. Copy, not geometry: moving a box does not change it.
    "thin_copy": Tier.COPY,
    # The picture readings all come out of one pass over the file's own shapes and the
    # bytes behind them. No render: the box and the crop are both declared.
    "figure_distortion": Tier.DECLARED,
    "figure_crop": Tier.DECLARED,
    "figure_undersized": Tier.DECLARED,
    "figure_mark_drift": Tier.DECLARED,
    "title_band_figure": Tier.DECLARED,
    "washed_backdrop": Tier.DECLARED,
    # The one reading that needs both truths at once: the file to say the lines were
    # meant flush, the render to show they are not. It cannot run without a render.
    "flush_drift": Tier.RENDERED,
    # The layout budgets read shapes and nothing else: no render, no copy. Two of them
    # are answers about the whole deck rather than a page, so they belong with the rows
    # a draft of three pages is not asked.
    "repeated_layout": Tier.DECLARED,
    "equal_card_habit": Tier.DECLARED,
    "symmetry_habit": Tier.DECLARED,
    # Where a page's own foot is, and whether the number in it is a field. Both come off
    # the declared geometry: the strip is the bottom of the canvas either way.
    "no_footer": Tier.DECLARED,
    "unnumbered_pages": Tier.DECLARED,
    # Which groups have an edge drawn around them, and which grids their columns come
    # off -- both read off the declared shapes.
    "grid_drift": Tier.DECLARED,
    # A declared box outside the canvas: `measure/layout.py` says the frame's origin is
    # not something the renderer negotiates.
    "off_page": Tier.DECLARED,
    # Copy on the layout's artwork and outside every placeholder, and `measure/
    # inherited.py` states it uses declared geometry so it still answers with no
    # LibreOffice on the box.
    "over_layout_art": Tier.DECLARED,
    # A `wrap=False` box narrower than its own line, decided against the copy's measured
    # width before anything is drawn.
    "spilled_copy": Tier.DECLARED,
    # Line count against box height from font metrics: `measure/fit.py` exists precisely
    # to answer this in the same round rather than one render later.
    "overset_copy": Tier.DECLARED,
    # `measure/width.py` calls this the one check a render cannot see -- a stacked label
    # overlaps nothing -- and the registry wires the words in only as a veto.
    "wrapped_label": Tier.DECLARED,
    # Whether the table still wears the Office gallery style and its banding flags (D20):
    # a GUID and two booleans in the XML.
    "native_table": Tier.DECLARED,
    # The same XML read for the row and column banding themselves.
    "banded_table": Tier.DECLARED,
    # Each column against the width its own widest cell needs. It reads the cell strings
    # to size them, but the verdict and the fix are both a column width.
    "wide_table": Tier.DECLARED,
    # The one row whose evidence is the program rather than the deck: `BuildOutcome`'s
    # per-page line spans. Free, and available on every build.
    "page_mapping": Tier.DECLARED,
    # A template photograph still on a finished page, matched against the prototype's
    # pictures by geometry and bytes.
    "template_picture": Tier.DECLARED,
    # The template's picture on a layout every page inherits: read off the layout, no
    # tolerance in it.
    "layout_picture": Tier.DECLARED,
    # The marks beside a page's units against each other and the template's bytes: the
    # file's own geometry and image parts, nothing rendered.
    "same_mark": Tier.DECLARED,
    # A cloned prototype page with new boxes laid over it: `adherence.py` finds it by the
    # shape positions that survive the deep copy.
    "template_underlay": Tier.DECLARED,
    # Whether the page's shapes still sit where the prototype its outline named put them.
    # Geometry against the plan.
    "prototype_kept": Tier.DECLARED,
    # The size the author declared, against the ramp -- deliberately the file's number
    # and not the render's, which is `type_floor`'s (D10).
    "type_scale": Tier.DECLARED,
    # Slide count against the brief: structural, and settled by the file alone.
    "page_budget": Tier.DECLARED,
    # The theme's colour scheme against the template's, which `house_style.py` picks
    # because it is the part a default cannot fake. One comparison, no render.
    "house_style": Tier.DECLARED,
    # Where each page's title box sits, compared across the deck. Declared geometry.
    "title_row": Tier.DECLARED,
    # Whether the deck opened, indexed, divided and closed in the template's own pages,
    # by the same surviving-coordinates measurement.
    "template_adherence": Tier.DECLARED,
    # Whether the composed pages came out as the same arrangement of the same kinds of
    # thing, read off the built shapes rather than off the plan.
    "layout_variety": Tier.DECLARED,
    # -- COPY: what the page says and what it credits. Reads strings, and what the deck
    # was given to read. A build that only moved boxes cannot have changed any of these.
    # The figure a page shows against the figure it names -- provenance of a claim.
    "citation": Tier.COPY,
    # A caption asserting a name the materials never print. It never opens the deck at
    # all: catalogue plus materials.
    "inferred_caption": Tier.COPY,
    # Which script the characters belong to, against the brief.
    "language": Tier.COPY,
    # A page printing a literal escape, which is one character in the author's program.
    "literal_escape": Tier.COPY,
    # An expression written as prose, found by reading the run text.
    "flat_formula": Tier.COPY,
    # Parallel claims stacked in one box, found by reading the paragraphs.
    "listed_claims": Tier.COPY,
    # Pages that are all prose, which is a question about what each page carries.
    "evidence": Tier.COPY,
    # The prototype's own filler text left in place: a string match against the
    # prototype's strings.
    "placeholder_copy": Tier.COPY,
    # -- RENDERED: needs the PDF's word boxes or type spans, because only there has the
    # renderer finished reflowing and autofitting.
    "word_collision": Tier.RENDERED,
    "clipped_copy": Tier.RENDERED,
    # Rule from the file, glyphs from the render; the finding is the intersection.
    "rule_strike": Tier.RENDERED,
    "card_overflow": Tier.RENDERED,
    "box_overflow": Tier.RENDERED,
    "crowded_panel": Tier.RENDERED,
    # A label the render broke one character short of fitting, which only the render
    # knows it did.
    "orphan_line": Tier.RENDERED,
    "unseparated_blocks": Tier.RENDERED,
    "excessive_whitespace": Tier.RENDERED,
    # The size a reader actually gets, after autofit -- D10: the boxes that matter state
    # no size at all, so the file answers nothing.
    "type_floor": Tier.RENDERED,
    # The same repeated slot rendered at several sizes: the render again, grouped across
    # pages.
    "type_drift": Tier.RENDERED,
    # -- RASTER: needs the rendered pixels.
    # The modal pixel under a text box is the only place the ground has an answer
    # (`contrast.py`), and it is the most expensive single check in the pass: 1.89s of a
    # measured 5.67s over a real 12-page deck.
    "unreadable": Tier.RASTER,
    # -- COVERAGE: not findings about the deck but about which rows above could run.
    # Never scheduled away: their whole purpose is that silence and "clean" are
    # different answers, and dropping them makes the reply claim the second.
    "unchecked_citations": Tier.COVERAGE,
    "unchecked_agreement": Tier.COVERAGE,
    "unrendered": Tier.COVERAGE,
}


def names(tier: Tier) -> frozenset[str]:
    """Every check in one tier."""
    return frozenset(name for name, row in TIERS.items() if row is tier)


# What `registry.DISPATCH` marks BLOCKING. Held here as a literal and asserted against
# that table, because this module's whole safety argument rests on it: a blocking row is
# never dropped from a batch that has the evidence for it.
BLOCKING_CHECKS = frozenset(
    {
        "citation",
        "page_budget",
        "language",
        "literal_escape",
        "word_collision",
        "house_style",
        "placeholder_copy",
        "template_underlay",
        "unreadable",
    }
)

# Checks that are not merely partial on an unfinished deck but wrong on one, so a draft
# is not held to them. Two of these are already exempt in `stages/build.py`
# (`_DRAFT_EXEMPT`); this adds the two that fail the same test. `title_row`, `type_drift`
# and `house_style` are deliberately absent -- three pages that disagree about the title
# row already disagree, and that is the finding D21 wants arriving early rather than at
# the finish line.
WHOLE_DECK_ONLY = frozenset(
    {
        # A budget is a fact about a finished deck: three of a draft's three pages
        # being card rows says nothing about what the deck will do.
        "equal_card_habit",
        "symmetry_habit",
        # A part-written deck has not broken the agreed length; it has not reached it.
        "page_budget",
        # Same argument, and already draft-exempt: pages the outline has not mapped yet.
        "page_mapping",
        # "Nearly all your pages are alike" is false of four pages out of a planned
        # twenty; the check self-silences under six, which is the same admission.
        "template_adherence",
        # A draft with no closing page has not failed to close in the template's own
        # page -- it has not got there.
        "layout_variety",
        # Feet are the last thing a script draws, and "half your pages have one" is not
        # a reading a three-page draft supports.
        "no_footer",
        "unnumbered_pages",
        # "Several of your pages leave a row of copy bare" is not a reading two drafted
        # pages support.
    }
)

# The checks whose answer comes off the render. With no PDF these already report nothing
# (`registry._rendered`), so dropping them changes no finding; what it changes is that a
# caller can see the render is not needed before paying for it.
NEEDS_RENDER = names(Tier.RENDERED) | names(Tier.RASTER)

# And the one check that runs without a render but is sharper with one: the registry
# hands `wrapped_label` the words as a veto rather than as its input.
SHARPER_WITH_RENDER = frozenset({"wrapped_label"})

# What this build changed, in the vocabulary of this route's call shape.
#
# The reference batching this is adapted from grades a batch of drawing ops. We have no
# such thing: the author writes one python-pptx program, `ppt_build` reruns all of it,
# and `slides=[3, 7]` selects which renders come back rather than which pages are built.
# So the vocabulary is what a rerun can be told apart by, and every value is decidable
# from what the tool already holds -- `seen.blocks_of` fingerprints each page's code
# block, `outcome.pages` gives the length, `draft` gives the last one. None of it is
# self-reported; D10's argument against trusting a declaration applies here too.
#
#   "nothing"    no page block's fingerprint changed and the prelude is byte-identical:
#                a rerun to page through renders, or D6's empty submission.
#   "prelude"    the shared setup changed -- imports, theme, template binding, a helper
#                constant -- so every page's geometry can have moved and no page's copy
#                has.
#   "pages"      one or more page blocks changed, at the same deck length.
#   "deck_shape" the page count changed, so every page below renumbers. Same checks as
#                "pages"; the difference is a fact about history, and `converged` is
#                where it is consumed.
#   "delivery"   the build that delivers (`draft=False`). Everything runs.
CHANGES = ("nothing", "prelude", "pages", "deck_shape", "delivery")

# Run on every build regardless of what changed: everything that can refuse the deck,
# plus the rows that say what could not be checked. A draft does not publish, so this is
# not what keeps publication fail-closed -- "delivery" running all 43 is -- but an
# author is owed a blocker in the round that introduced it.
_ALWAYS = BLOCKING_CHECKS | names(Tier.COVERAGE)


def checks_for(changed: str, *, has_render: bool) -> frozenset[str]:
    """The checks worth running on a build that changed `changed`.

    A name absent from the result is a check not run on this build, which is not the
    same as a check that passed. A caller that shortens the reply this way owes the
    author the previous build's findings for the rows it skipped -- on `"nothing"` the
    file is the file that was already measured, and a reply that silently drops its
    warnings reads as a deck that fixed them.

    Raises `ValueError` on a value outside `CHANGES`, rather than defaulting: a typo
    that quietly selected the smallest batch would drop checks nobody chose to drop.
    """
    if changed not in CHANGES:
        raise ValueError(f"unknown change {changed!r}; one of {', '.join(CHANGES)}")
    if changed == "delivery":
        wanted = frozenset(TIERS)
    elif changed == "nothing":
        wanted = _ALWAYS
    elif changed == "prelude":
        wanted = _ALWAYS | names(Tier.DECLARED) | NEEDS_RENDER
    else:
        wanted = frozenset(TIERS)
    if changed != "delivery":
        wanted -= WHOLE_DECK_ONLY
    if not has_render:
        wanted -= NEEDS_RENDER
    return frozenset(wanted)


# How many consecutive builds may carry one unchanged finding before rebuilding for it
# is judged to be answering nothing. One report plus two attempts: the first build states
# the finding, and the author gets the two repair rounds this route's hard-convergence
# budget allows. A third identical reply is the evidence that neither attempt reached it,
# which is what happened for nineteen minutes of build time on the run in the module
# docstring. Deliberately not tuned per kind -- a per-kind ceiling is the exemption list
# D17 says a measurement resorts to when it cannot tell what it is looking at.
UNMOVED_BUILDS = 3

# What `registry._one_per_cause` prepends when one cause is reported on several pages.
# Stripped so the folded and unfolded forms of the same finding compare equal: without
# it, a cause that spreads from two pages to three reads as an entirely new finding and
# the streak restarts on a build that changed nothing.
_FOLDED = re.compile(r"^pages [\d,\s]+ all report this, so it is one cause and not \d+: ")

Cause = tuple[str, int | None, str]


@dataclass(frozen=True)
class Reported:
    """One finding as convergence sees it: what, where, and whether it refuses."""

    kind: str
    page: int | None
    message: str
    blocking: bool = False

    @property
    def cause(self) -> Cause:
        return (self.kind, self.page, self.message)


@dataclass(frozen=True)
class BuildRecord:
    """What one build came back with.

    `pages` is the deck's length at that build. A build at a different length is not
    comparable to this one -- insert a page and every page below renumbers, so page 7's
    findings would be weighed against what is now a different page. `services/regress.py`
    re-baselines on exactly this and reports nothing; the same rule is applied here by
    walking back no further than the last length change.
    """

    findings: tuple[Reported, ...] = ()
    pages: int | None = None
    built: bool = True
    """False when the program raised and produced no deck at all: nothing was measured,
    so the build is neither progress nor a repeat."""

    @classmethod
    def of(
        cls,
        findings: Iterable[object],
        *,
        pages: int | None = None,
        built: bool = True,
        blocking_kinds: Iterable[str] = (),
    ) -> BuildRecord:
        """A record from real `Finding`s, with folded causes expanded back per page."""
        fatal = frozenset(blocking_kinds)
        out: list[Reported] = []
        for finding in findings:
            kind = str(getattr(finding, "kind", ""))
            message = _FOLDED.sub("", str(getattr(finding, "message", "")))
            refuses = getattr(finding, "severity", None) is Severity.BLOCKING or kind in fatal
            page = getattr(finding, "page", None)
            detail = getattr(finding, "detail", None) or {}
            folded = detail.get("on_pages") if isinstance(detail, Mapping) else None
            if page is None and isinstance(folded, Sequence) and not isinstance(folded, str | bytes):
                out.extend(Reported(kind, int(one), message, refuses) for one in folded)
                continue
            out.append(Reported(kind, page if page is None else int(page), message, refuses))
        return cls(findings=tuple(out), pages=pages, built=built)


def _comparable(history: Sequence[BuildRecord]) -> list[BuildRecord]:
    """The trailing run of builds that produced a deck at the current length."""
    built = [record for record in history if record.built]
    if not built:
        return []
    length = built[-1].pages
    run: list[BuildRecord] = []
    for record in reversed(built):
        if length is not None and record.pages is not None and record.pages != length:
            break
        run.append(record)
    return list(reversed(run))


def unmoved(history: Sequence[BuildRecord]) -> dict[Cause, int]:
    """For each cause standing in the last build, how many builds have carried it as is.

    Per cause rather than per deck, because the two answer different questions and a
    caller wants both: whether to stop rebuilding at all, and which particular finding to
    stop answering while the rest of the work continues.
    """
    run = _comparable(history)
    if not run:
        return {}
    seen = [frozenset(f.cause for f in record.findings) for record in run]
    streaks: dict[Cause, int] = {}
    for finding in run[-1].findings:
        count = 0
        for present in reversed(seen):
            if finding.cause not in present:
                break
            count += 1
        streaks[finding.cause] = count
    return streaks


def converged(history: Sequence[BuildRecord]) -> tuple[bool, str]:
    """Whether rebuilding has stopped answering anything, and why, for the author.

    True means stop: the deck is not moving, and another build will return this reply.
    It is never returned while something refuses the deck -- a deck that cannot be
    published is not a deck to stop rebuilding, however stuck the loop looks.
    """
    run = _comparable(history)
    if not run:
        return False, "no build has produced a deck yet, so there is nothing to weigh one against"
    last = run[-1]
    refusing = sorted({f.kind for f in last.findings if f.blocking})
    if refusing:
        said = ", ".join(refusing)
        return False, f"{said} still refuses this deck, and a deck that cannot be published has to be rebuilt"
    if not last.findings:
        return True, "this build reports nothing"
    if len(run) < UNMOVED_BUILDS:
        return False, f"only {len(run)} comparable build(s) so far, and it takes {UNMOVED_BUILDS} to call it"
    recent = [frozenset(f.cause for f in record.findings) for record in run[-UNMOVED_BUILDS:]]
    if any(one != recent[-1] for one in recent):
        return False, "the findings changed between these builds, so the last edits are still reaching them"
    streaks = unmoved(history)
    said = "; ".join(_one_cause(kind, streaks) for kind in sorted({cause[0] for cause in streaks}))
    return True, (
        f"the last {UNMOVED_BUILDS} builds came back with the same findings and nothing else moved: {said}. "
        "Rebuilding is not reaching them -- look at the pages, and either ship the deck or change the approach "
        "rather than building again for the same reply"
    )


def _one_cause(kind: str, streaks: Mapping[Cause, int]) -> str:
    """One kind, the pages it stands on, and how long it has stood there."""
    mine = {cause: count for cause, count in streaks.items() if cause[0] == kind}
    pages = sorted(page for (_, page, _) in mine if page is not None)
    longest = max(mine.values())
    where = f" on page(s) {', '.join(str(page) for page in pages)}" if pages else ""
    return f"{kind}{where}, unchanged for {longest} build(s)"


class Unit(Enum):
    """What a floor is measured in.

    Points are the canonical length here for the reason `measure/rendered.py` gives: the
    file's shape rectangles and the render's word boxes share a point coordinate system
    with its origin at the top left, so they compare without a transform.
    """

    POINT = "pt"
    INCH = "in"
    EMU = "emu"
    SQUARE_INCH = "in2"
    RATIO = "ratio"
    SHARE = "share"
    LINES = "lines"
    COUNT = "count"


EMU_PER_POINT = 12700
EMU_PER_INCH = 914400
# The render's own pixel, which is what a floor borrowed from a screenshot-based
# reference has to be converted through. `services/render/pdf.py` defaults to 144dpi
# because it puts a 16:9 page at exactly 1920x1080, so one rendered pixel is half a
# point, one 144th of an inch, 6350 EMU -- which is `EDGE_SLACK_EMU` exactly. The
# reference's "ignore sub-pixel differences" is a floor this repo already holds.
POINTS_PER_RENDERED_PIXEL = 0.5


@dataclass(frozen=True)
class Floor:
    """One difference judged too small to report, and where that judgement lives."""

    value: float
    unit: Unit
    covers: tuple[str, ...]
    source: str
    why: str
    in_force: bool = True
    """False for a floor proposed here and not yet held anywhere in the code. Nothing in
    this module changes a threshold; a proposed row is a recommendation to whoever wires
    it, and it is marked so it cannot be read as current behaviour."""


# Every floor already in force, in one table, converted into one set of units. Collected
# rather than invented: each row names the constant it mirrors, and the test asserts the
# two are equal, so a threshold retuned in its own module fails a test here instead of
# leaving this table quietly wrong.
NOISE_FLOOR: Mapping[str, Floor] = {
    "canvas_edge": Floor(
        6350,
        Unit.EMU,
        ("off_page",),
        "measure.layout.EDGE_SLACK_EMU",
        "half a point, one rendered pixel at 144dpi: a rule drawn on the margin rounds either way",
    ),
    "data_mark": Floor(
        4572,
        Unit.EMU,
        ("band",),
        "gates.bands.MARK_TOLERANCE_EMU",
        "0.005in: how far apart two segments of one bar may sit and still be one bar",
    ),
    "card_rim": Floor(
        3.0,
        Unit.POINT,
        ("card_overflow",),
        "measure.rendered.CARD_SLOP_PT",
        "what a border stroke and antialiasing account for",
    ),
    "card_padding": Floor(
        4.0,
        Unit.POINT,
        ("crowded_panel",),
        "measure.rendered.CARD_PADDING_PT",
        "under a sixteenth of an inch: below it the type touches the rim rather than sitting in it",
    ),
    "same_line": Floor(
        4.0,
        Unit.POINT,
        ("wrapped_label",),
        "measure.layout._SAME_LINE_PT",
        "how far two rendered words may differ vertically and still be one line",
    ),
    "hairline": Floor(
        4.5,
        Unit.POINT,
        ("rule_strike", "band"),
        "measure.rendered.RULE_MAX_HEIGHT_PT",
        "the ceiling a template's own kicker rule was refused by an eighth of a millimetre for missing (D17)",
    ),
    "cloned_shape": Floor(
        0.05,
        Unit.INCH,
        ("template_adherence", "template_underlay", "prototype_kept", "template_picture"),
        "measure.adherence.TOLERANCE_IN",
        "how far a cloned shape may drift and still count as the prototype's",
    ),
    "coverable_area": Floor(
        0.05,
        Unit.SQUARE_INCH,
        ("covered_shape",),
        "measure.overlap.MIN_AREA_IN",
        "a shape smaller than this is not worth reporting as hidden",
    ),
    "title_row_left": Floor(
        0.2,
        Unit.INCH,
        ("title_row",),
        "gates.house_style.TITLE_DRIFT_IN",
        "how far a page's title may sit from the row the deck settled on",
    ),
    "title_row_width": Floor(
        0.6,
        Unit.INCH,
        ("title_row",),
        "gates.house_style.TITLE_WIDTH_DRIFT_IN",
        "and how much narrower or wider, which tolerates a title box sized to its own words",
    ),
    "same_band": Floor(
        0.6,
        Unit.INCH,
        ("layout_variety",),
        "measure.variety.BAND_TOLERANCE_IN",
        "how far two pages' regions may differ and still be the same arrangement",
    ),
    "render_drift": Floor(
        1.03,
        Unit.RATIO,
        ("overset_copy",),
        "measure.fit.RENDER_DRIFT_HEADROOM",
        "3% of the width, about 20x the 0.14% drift measured between the measurer and LibreOffice 7.4",
    ),
    "no_wrap_slack": Floor(
        1.02,
        Unit.RATIO,
        ("spilled_copy",),
        "measure.layout.NO_WRAP_SLACK",
        "2%, because the measurer's font is not the renderer's",
    ),
    "label_slack": Floor(
        1.05,
        Unit.RATIO,
        ("wrapped_label",),
        "measure.layout.LABEL_SLACK",
        "5%: a short label leaves no room to wrap gracefully, so report only a clear miss",
    ),
    "type_drift": Floor(
        1.12,
        Unit.RATIO,
        ("type_drift",),
        "measure.type_size.DRIFT_RATIO",
        "12% between two copies of one slot before the pair reads as inconsistent",
    ),
    "collision_share": Floor(
        0.4,
        Unit.SHARE,
        ("word_collision",),
        "measure.rendered.COLLISION_SHARE",
        "below it a generous box's word merely grazes its neighbour and no reader sees a collision",
    ),
    "covered_share": Floor(
        0.6,
        Unit.SHARE,
        ("covered_shape",),
        "measure.overlap.COVERED",
        "how much of a shape has to be behind something before the page is not showing it",
    ),
    "outside_line_share": Floor(
        0.5,
        Unit.SHARE,
        ("box_overflow",),
        "measure.rendered.OUTSIDE_LINE_SHARE",
        "half a line: the deepest legitimate tail measured was 0.12 of a line, the escaped one 1.3",
    ),
    "overset_slack": Floor(
        1,
        Unit.LINES,
        ("overset_copy",),
        "measure.fit.OVERSET_SLACK_LINES",
        "one whole line over the box's capacity before the copy is called overset",
    ),
    "collisions_per_page": Floor(
        4,
        Unit.COUNT,
        ("word_collision",),
        "measure.rendered.COLLISIONS_PER_PAGE",
        "one broken card makes a dozen pairwise hits and the fix is the card, not the dozen",
    ),
    "overflows_per_page": Floor(
        4,
        Unit.COUNT,
        ("card_overflow", "box_overflow"),
        "measure.rendered.OVERFLOWS_PER_PAGE",
        "same argument, applied to a card whose every line has escaped",
    ),
    "rules_per_page": Floor(
        3,
        Unit.COUNT,
        ("rule_strike",),
        "measure.rendered.RULES_PER_PAGE",
        "same argument again, applied to one misplaced divider crossing a paragraph",
    ),
    # The one row this module proposes rather than reports. The reference it comes from
    # ignores spacing differences of four screen pixels or less; at this pipeline's
    # 144dpi that is two points, and nothing currently holds a general spacing floor --
    # `unseparated_blocks` and `excessive_whitespace` both compare gaps with no minimum
    # difference at all. Marked as not in force: wiring it changes what those two report,
    # which is a threshold decision and not this module's to take.
    "spacing_noise": Floor(
        2.0,
        Unit.POINT,
        ("unseparated_blocks", "excessive_whitespace"),
        "",
        "4 rendered pixels at 144dpi: a gap difference this small is not one a reader sees",
        in_force=False,
    ),
}

_LENGTHS: Mapping[Unit, float] = {
    Unit.POINT: 1.0,
    Unit.INCH: 72.0,
    Unit.EMU: 1.0 / EMU_PER_POINT,
}


def in_points(name: str) -> float | None:
    """A floor in points, or None when it is not a length.

    A ratio, a share, a count and a square inch have no point value, and returning a
    number for them would invite a comparison that means nothing.
    """
    floor = NOISE_FLOOR[name]
    scale = _LENGTHS.get(floor.unit)
    return None if scale is None else floor.value * scale


def in_rendered_pixels(name: str) -> float | None:
    """The same floor in the render's own pixels at 144dpi, for a screen-shaped rule."""
    points = in_points(name)
    return None if points is None else points / POINTS_PER_RENDERED_PIXEL
