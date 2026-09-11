"""What size a built page's copy ends up at, and the floor it has to clear.

Almost everything else about a page is better judged by looking at it: a pass
holding the render sees a hole in the layout, uneven cards or a figure fighting
its caption more reliably than any measurement of the pixels. Type size is the
exception, and not because it is subtle -- because a bitmap carries no ruler. At
the size a page is reviewed, 11pt and 16pt both look fine, and a deck whose body
copy sits at 11.5pt reads as perfectly legible right up until it is projected in
a room.

The first version measured it off the .pptx and called that exact. It is exact
about the wrong number. A template's text box carries `<a:normAutofit/>` -- shrink
the text until it fits -- and often no size at all, so the file states nothing and
the renderer decides. On a live deck built from a six-card prototype, four cards
holding one sentence each came out at 11.7, 13.7, 13.5 and 10.8pt on page 8 and
13.5, 13.5, 13.5 and 11.1pt on page 10, out of one 18pt slot. No run in any of
those boxes declares a size, so the census skipped every one of them and both
pages were reported clean while a reader could not read either.

So the size that matters is read off the render, where autofit has already
happened, and the file's own number is kept beside it: the gap between the two is
the finding's explanation. Nobody chose 10.8pt -- a box too small for its copy did,
and the fix is the box or the copy rather than the size.

That same pair of numbers answers the other half of the complaint, which is not
that one page is small but that pages disagree. One slot repeated across a deck
has one size, and a reader reads a page against the page before it; eight cards
set at five sizes reads as unfinished however legible each one is. `drift_findings`
groups the boxes a deck repeats -- same declared size, same shape, wherever they
are -- and reports the ones the renderer set apart from the rest.

Both of those read the render. `scale_findings` reads the file, because it asks a
different question: not what a reader gets but what the author chose. `ppt_layout`
hands an author a ramp -- `BODY_PT` 16, `LABEL_PT` 14, and the steps above -- and
across ten live pages those constants were named zero times; every page picked its own
integer, thirteen distinct values from 10 to 32pt. The floor cannot see that, because
a page whose copy is set at 15pt is legible, deliberate-looking and one point under
what its own deck calls body: 15 of the 43 copy blocks measured across 34 generated
decks are at 15pt, more than at any other size, and `type_floor` reports none of them.
So the division of labour is the floor itself. Under it, `type_floor` already says
"bring it up" about the same box; between the floor and `BODY_PT`, at a size the ramp
does not have, nothing said anything at all.

Two more readings sit here, both about a page the author cloned rather than drew, and
both asking about a *pair* of boxes rather than one. `drift_findings` above groups one
slot's copies deck-wide and measures each against the size the slot was drawn at, which
answers "did this box shrink" and not "do the boxes beside each other agree" -- on one
delivered page it named the four card headings that came out at 16.8pt as the outliers
and treated the lone 20.0pt as the house style, and on the page after it reported all
five members of a row that renders uniformly at 17.4pt, where a reader sees nothing
wrong at all. The row's own rendered spread is the missing question, not a missing
input, and `row_findings` asks it. `outranked_findings` asks the other one: whether the
page's title still outranks the line under it, which on a cloned page is a number the
author was never shown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_ppt.contracts.findings import Finding, Severity

# The one comparison that decides whether a shape came from the template, with the
# tolerance it was calibrated at. Private and imported anyway: the alternative is a
# fourth copy of "is this box the template's", and the reason `adherence` owns it is
# that the answer took three live decks to calibrate.
from raven_ppt.services.measure.adherence import _matches
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    Rect,
    iter_shapes,
    iter_text_frames,
    open_deck,
    shape_rect_pt,
)

# How this repo already answers "which shapes on this page are siblings" and "which of
# them is the title". Both are private and imported anyway, for the reason `_matches` is:
# `units` is the operation a template page exists for -- `fill` writes through it, so the
# groups it returns are the groups the author counted off the render -- and `_heading_rows`
# resolves a title on all 197 example pages of the twelve templates by reading what the
# template named before inferring anything from where it sits. A second answer to either
# would disagree with the one the author is working from.
from raven_ppt.services.template.compose import _all_shapes, _heading_rows, units
from raven_ppt.services.template.decompile import inherited_size, page_design

# Body copy has to hold up projected, not just on the screen the deck was made
# on. BODY_FLOOR_PT is the floor for the size a page mostly runs at;
# MIN_FLOOR_PT is the floor for anything at all -- a caption, a table cell, a
# source line.
#
# Plain numbers because a slide is 7.5in tall: 4:3 is 10x7.5, 16:9 is 13.33x7.5,
# and they differ only in width. This was expressed as a share of page height
# for a while, which bought a scaling factor nobody ever used and hid the two
# numbers that actually apply behind a multiplication.
#
# Where they come from: a reviewed deck whose body ran 10-13.5pt with 10pt
# captions was marked down for legibility, so that band is known to fail. 14pt
# is a step above it rather than a measured threshold -- pinning it exactly
# wants one experiment, the same deck built at 12 / 14 / 16 / 18pt and judged on
# legibility alone.
BODY_FLOOR_PT = 14.0
MIN_FLOOR_PT = 10.8

# Text this short is a mark on a chart or a page number, not copy: a deck sets
# axis labels and callout letters small on purpose, and holding them to the body
# floor would flag every chart in the deck.
_INCIDENTAL_CHARS = 3


def type_floors(height_in: float | None = None) -> tuple[float, float]:
    """(body floor, absolute floor) in points.

    Takes the page height and ignores it: every PowerPoint canvas is 7.5in tall.
    The parameter stays so a deck that genuinely is not -- a 5.625in canvas out
    of Google Slides, where these numbers would read a third too large -- has an
    obvious place to be handled when one turns up.
    """
    return BODY_FLOOR_PT, MIN_FLOOR_PT


@dataclass(frozen=True)
class Span:
    """One run of type as the renderer actually set it: its size and where it landed.

    Read off the PDF rather than the .pptx because autofit, font substitution and
    line breaking have all happened by then -- the same reason `words` is read off
    the render. Points with the origin top left, which is the .pptx's own system,
    so a span and a shape's declared box compare directly.
    """

    page: int
    size_pt: float
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def rect(self) -> Rect:
        return Rect(self.x0, self.y0, self.x1, self.y1)


def rendered_spans(pdf_path: Path) -> list[Span] | None:
    """Every span of type in a rendered deck, or None when it cannot be read.

    None and empty differ and callers act on the difference: no PyMuPDF on the box
    means no signal, and a check with no signal reports nothing rather than
    reporting that every page is fine.
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover -- older installs only expose the old name
        try:
            import fitz as pymupdf
        except ImportError:
            return None
    found: list[Span] = []
    try:
        with pymupdf.open(str(pdf_path)) as document:
            for number, page in enumerate(document, start=1):
                for block in page.get_text("dict").get("blocks", ()):
                    for line in block.get("lines", ()):
                        for span in line.get("spans", ()):
                            text = span.get("text", "")
                            if not text.strip():
                                continue
                            box = span.get("bbox") or (0, 0, 0, 0)
                            found.append(
                                Span(
                                    page=number,
                                    size_pt=round(float(span.get("size") or 0), 2),
                                    text=text,
                                    x0=box[0],
                                    y0=box[1],
                                    x1=box[2],
                                    y1=box[3],
                                )
                            )
    except Exception:  # noqa: BLE001 -- an unreadable render is no signal, not a defect
        return None
    return found


@dataclass(frozen=True)
class TypeCensus:
    """What sizes one page's copy came out at, in points.

    The raw measurement, kept separate from the finding built out of it: a
    review stage wants to show the numbers, and a gate wants a sentence.

    `body_pt` is the size the reader gets -- off the render when there is one --
    and `declared_pt` is what the file says. They differ when a box shrinks its
    own text to fit, which is the common case inside a template.
    """

    page: int
    height_in: float
    body_pt: float | None
    smallest_pt: float | None
    chars: int
    below_body_floor: int
    below_hard_floor: int
    declared_pt: float | None = None

    @property
    def floors(self) -> tuple[float, float]:
        return type_floors(self.height_in)

    @property
    def under_floor(self) -> bool:
        body_floor, _ = self.floors
        return self.body_pt is not None and self.body_pt < body_floor

    @property
    def shrunk(self) -> bool:
        """The renderer set the copy smaller than the file asked for."""
        return self.body_pt is not None and self.declared_pt is not None and self.body_pt < self.declared_pt * 0.97

    def detail(self) -> dict[str, Any]:
        body_floor, hard_floor = self.floors
        payload: dict[str, Any] = {"page": self.page, "body_floor_pt": body_floor}
        if self.body_pt is not None:
            payload["body_pt"] = self.body_pt
        if self.declared_pt is not None:
            payload["declared_pt"] = self.declared_pt
        if self.smallest_pt is not None:
            payload["smallest_pt"] = self.smallest_pt
        if self.chars:
            payload["under_body_floor"] = round(self.below_body_floor / self.chars, 3)
        if self.below_hard_floor:
            payload["under_hard_floor_pt"] = hard_floor
        return payload


def _weighted(sizes: dict[float, int]) -> float | None:
    """The size most of the characters are set at, ties going to the larger."""
    return max(sizes, key=lambda size: (sizes[size], -size)) if sizes else None


def _declared_sizes(slide) -> dict[float, int]:
    """What the .pptx itself states, by character count. Silent about what it omits."""
    sizes: dict[float, int] = {}
    for frame in iter_text_frames(slide):
        for para in frame.paragraphs:
            for run in para.runs:
                size = run.font.size or para.font.size
                length = len(run.text.strip())
                if size is None or length <= _INCIDENTAL_CHARS:
                    continue
                key = round(size.pt, 1)
                sizes[key] = sizes.get(key, 0) + length
    return sizes


def census(pptx_path: Path, spans: Sequence[Span] | None = None) -> list[TypeCensus]:
    """Measure the type on every page of a built deck.

    With `spans` -- the render's own type -- the sizes are the ones a reader gets.
    Without them the file's declared sizes are all there is, which is the fallback
    rather than the intent: it cannot see a box that shrank its copy to fit.
    """
    presentation = open_deck(pptx_path)
    height_in = presentation.slide_height / EMU_PER_INCH
    body_floor, hard_floor = type_floors(height_in)
    by_page: dict[int, dict[float, int]] = {}
    for span in spans or ():
        length = len(span.text.strip())
        if length <= _INCIDENTAL_CHARS or not span.size_pt:
            continue
        page = by_page.setdefault(span.page, {})
        key = round(span.size_pt, 1)
        page[key] = page.get(key, 0) + length
    measured: list[TypeCensus] = []
    for number, slide in enumerate(presentation.slides, start=1):
        declared = _declared_sizes(slide)
        sizes = by_page.get(number, declared) if spans is not None else declared
        measured.append(
            TypeCensus(
                page=number,
                height_in=height_in,
                body_pt=_weighted(sizes),
                smallest_pt=min(sizes) if sizes else None,
                chars=sum(sizes.values()),
                below_body_floor=sum(count for size, count in sizes.items() if size < body_floor),
                below_hard_floor=sum(count for size, count in sizes.items() if size < hard_floor),
                declared_pt=_weighted(declared),
            )
        )
    return measured


# Raising a size costs room, the room comes from the copy, and a gate that
# refused publication until the floor was met could be answered by shrinking the
# copy back -- which is the oscillation D2 describes, ending with no deck at all.
# So this reports and never refuses, and the room comes from the page.
_FIX = (
    "Bring it up, and give the copy the room the larger size needs rather than letting it overflow: "
    "a taller band, a wider column, less copy, or the page split. Do not shrink it back to fit"
)
# What to do instead when nobody chose the size: the box did, and the box is the
# thing to change.
_AUTOFIT_FIX = (
    "Nothing set that size -- the box is set to shrink its text until it fits and the copy is too long "
    "for it. Give the box the height the copy needs (place it, or take the room from a neighbour), or "
    "say it in fewer words"
)


def type_findings(pptx_path: Path, spans: Sequence[Span] | None = None) -> list[Finding]:
    """Pages whose type is under a floor.

    Reported per page rather than as a deck-wide verdict: the fix is per page, and a
    deck is rarely wrong everywhere. With the render in hand the offending boxes are
    named, because "this page runs at 10.8pt" and "these three boxes shrank to 10.8pt
    while the rest of the page is fine" ask for different fixes.
    """
    if spans:
        return _slot_findings(pptx_path, spans)
    findings: list[Finding] = []
    for page in census(pptx_path, None):
        body_floor, hard_floor = page.floors
        if page.body_pt is None:
            continue
        if page.under_floor:
            problem = f"body copy is set at {page.body_pt}pt, under the {body_floor}pt floor"
        elif page.smallest_pt is not None and page.smallest_pt < hard_floor:
            problem = (
                f"its smallest copy is set at {page.smallest_pt}pt, under the {hard_floor}pt floor that "
                "holds for captions and table cells too"
            )
        else:
            continue
        findings.append(
            Finding(
                kind="type_floor",
                severity=Severity.WARNING,
                page=page.page,
                message=f"{problem}. {_FIX}",
                detail=page.detail(),
            )
        )
    return findings


def _slot_findings(pptx_path: Path, spans: Sequence[Span]) -> list[Finding]:
    """The boxes whose copy the reader gets under the floor, named one page at a time."""
    presentation = open_deck(pptx_path)
    body_floor, hard_floor = type_floors(presentation.slide_height / EMU_PER_INCH)
    by_page: dict[int, list[tuple[Slot, float]]] = {}
    # Grouped before any of them is classified, because one slot's floor depends on
    # what else is on its page: a kicker is only a kicker over a title.
    everything: dict[int, list[Slot]] = {}
    for slot in slots(pptx_path, spans):
        everything.setdefault(slot.page, []).append(slot)
    for here in everything.values():
        for slot in here:
            if slot.rendered_pt is None or slot.chars < _SLOT_CHARS:
                continue
            if _is_footer(slot, presentation):
                floor = FOOTER_FLOOR_PT
            elif _is_kicker(slot, here):
                floor = hard_floor
            else:
                copy = slot.chars >= _COPY_CHARS and not _is_caption(slot, presentation)
                floor = body_floor if copy else hard_floor
            if slot.rendered_pt >= floor:
                continue
            by_page.setdefault(slot.page, []).append((slot, floor))
    findings: list[Finding] = []
    for page, here in sorted(by_page.items()):
        # Each box against the floor that applies to it: a body block, a caption and a
        # footer answer to three different numbers, and naming one of them for all of
        # them is how a page reported a 9.5pt running credit as body copy set too small.
        worst = sorted(here, key=lambda pair: pair[0].rendered_pt)
        named = ", ".join(f"{slot.rendered_pt:g}pt under {floor:g}pt ('{slot.head}')" for slot, floor in worst[:3])
        more = f" and {len(worst) - 3} more" if len(worst) > 3 else ""
        chose = [slot for slot, _ in worst if slot.declared_pt is not None and slot.declared_pt < body_floor]
        findings.append(
            Finding(
                kind="type_floor",
                severity=Severity.WARNING,
                page=page,
                message=(
                    f"{len(worst)} box(es) on this page show their copy under the floor: {named}{more}. "
                    + (_FIX if chose else _AUTOFIT_FIX)
                ),
                detail={
                    "page": page,
                    "body_floor_pt": body_floor,
                    "floors_pt": [floor for _, floor in worst],
                    "sizes_pt": [slot.rendered_pt for slot, _ in worst],
                    "declared_pt": [slot.declared_pt for slot, _ in worst],
                    "boxes_in": [list(slot.shape) for slot, _ in worst[:3]],
                },
            )
        )
    return findings


# How far two settings of one slot can drift before it reads as an accident rather
# than a decision. A type scale steps by about 1.2x, so 12% sits inside one step:
# 13.5pt against 14.8pt is one size measured twice, and 13.5 against 18 is not.
DRIFT_RATIO = 1.12
# Under this many characters a box holds a mark -- a step number, a page number --
# and the deck sets those small on purpose.
_SLOT_CHARS = 4
# A caption says so, in both languages every deck measured writes them in. A source
# line and a figure caption are the two things a deck sets smallest on purpose, and
# holding them to the body floor put the same finding on seven pages of one deck --
# "来源：TarViS 原论文（CVPR 2023）" at 11pt, which is legible, deliberate and 24
# characters long, so no length rule could tell it from copy.
_CAPTION_MARKERS = ("来源", "资料来源", "注：", "图", "表", "source:", "figure", "fig.", "table", "note:")
# And the band at the foot of a page where those live. A body block starts higher than
# this on every page measured; below it a line is furniture.
_FOOTER_BAND = 0.9
# What the foot of a page may run at. Furniture is not copy: nobody reads a running
# credit from a seat, and the one person who wants it walks up to the screen. Holding
# it to the 10.8pt floor meant every page of a deck reported its own footer -- one run
# collected 210 type_floor findings that were two lines repeated, "Source: ..." and
# "TarViS · CVPR 2023 · arXiv:2301.02657", both at a perfectly ordinary 9.5pt. Under
# 8pt it stops being legible even up close, and that is worth saying.
FOOTER_FLOOR_PT = 8.0

# And under this it holds a label rather than copy: a byline, a unit, a chart's axis.
# The body floor is about copy that has to hold up projected, and applying it to every
# short line made both models fight the same finding on their cover -- "公司内部技术评审"
# at 12pt is a byline, and enlarging it is not an improvement. The hard floor still
# applies to these; nothing on a page may go under it.
_COPY_CHARS = 20


@dataclass(frozen=True)
class Slot:
    """One text box, its declared shape, and the size the renderer set it at."""

    page: int
    box: Rect
    declared_pt: float | None
    rendered_pt: float | None
    chars: int
    head: str

    @property
    def shape(self) -> tuple[float, float]:
        """Width and height in inches, rounded the way clones of one slot agree."""
        return (round(self.box.width / 72, 2), round(self.box.height / 72, 2))


def slots(pptx_path: Path, spans: Sequence[Span]) -> list[Slot]:
    """Every text box on every page, with the size its copy came out at.

    Spans are attributed to boxes by where they landed and what they say: the box
    has to contain the span's centre, and where several do -- a template stacks a
    numbered bubble behind its body copy -- the one whose own text holds the span's
    text wins, then the tightest box. Position alone attributed '01' to the sentence
    printed over it, which is the one pair on the page guaranteed to differ in size.
    """
    presentation = open_deck(pptx_path)
    by_page: dict[int, list[Span]] = {}
    for span in spans:
        by_page.setdefault(span.page, []).append(span)
    found: list[Slot] = []
    for number, slide in enumerate(presentation.slides, start=1):
        boxes: list[tuple[Any, Rect, str]] = []
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False) or shape.left is None:
                continue
            text = shape.text_frame.text.strip()
            if not text:
                continue
            # `shape_rect_pt` walks the groups above it: a grouped shape's own numbers
            # are in its group's coordinate space, and a span was being attributed to
            # whichever box happened to be reported at that point.
            boxes.append(
                (
                    shape,
                    shape_rect_pt(shape),
                    text,
                )
            )
        sizes: list[dict[float, int]] = [{} for _ in boxes]
        for span in by_page.get(number, ()):
            index = _owner(span, boxes)
            if index is None:
                continue
            length = len(span.text.strip())
            key = round(span.size_pt, 1)
            sizes[index][key] = sizes[index].get(key, 0) + length
        for (shape, box, text), measured in zip(boxes, sizes):
            found.append(
                Slot(
                    page=number,
                    box=box,
                    declared_pt=_declared_pt(shape),
                    rendered_pt=_weighted(measured),
                    chars=len(text),
                    head=_head(text),
                )
            )
    return found


def _owner(span: Span, boxes: Sequence[tuple[Any, Rect, str]]) -> int | None:
    centre = ((span.x0 + span.x1) / 2, (span.y0 + span.y1) / 2)
    holding = [
        index
        for index, (_, box, _) in enumerate(boxes)
        if box.x0 - 1 <= centre[0] <= box.x1 + 1 and box.y0 - 1 <= centre[1] <= box.y1 + 1
    ]
    if not holding:
        return None
    said = span.text.strip()
    saying = [index for index in holding if said and said in boxes[index][2]]
    candidates = saying or holding
    return min(candidates, key=lambda index: boxes[index][1].area)


def _declared_pt(shape) -> float | None:
    sizes = [
        run.font.size.pt
        for para in shape.text_frame.paragraphs
        for run in para.runs
        if run.font.size is not None and run.text.strip()
    ]
    return round(max(sizes), 1) if sizes else None


def _is_footer(slot: Slot, presentation: Any) -> bool:
    """In the band at the foot of the page, where a deck puts its furniture."""
    canvas = (presentation.slide_height or 0) / EMU_PER_POINT
    return bool(canvas and slot.box.y0 >= canvas - _FOOTER_BAND * 72)


# The line over a page's title, naming the section the page belongs to. `heading`
# sets it at `KICKER_PT`, which is 12 against a 14pt body floor, and exposes no
# parameter for it -- so held to the body floor it reports every page any deck gives
# a top edge to, and names a fix (bring the size up, give the copy more room) that
# the author has no way to carry out. No length rule tells it from copy either: the
# kickers measured run past `_COPY_CHARS`. What tells it is where it sits.
_HEAD_BAND = 1.6
# How much larger the title it labels is set. A type scale steps by about 1.2x and a
# title is more than one step over its kicker, so this clears two boxes of body copy
# that merely differ while still catching the pair.
_TITLE_RATIO = 1.4


def _is_kicker(slot: Slot, here: Sequence[Slot]) -> bool:
    """The small line at the head of a page, over a title set materially larger."""
    if slot.rendered_pt is None or slot.box.y0 > _HEAD_BAND * 72:
        return False
    return any(
        other is not slot
        and other.rendered_pt is not None
        and other.rendered_pt >= slot.rendered_pt * _TITLE_RATIO
        # Below this one, and sharing some of its width: the title it labels, not a
        # display number somewhere else in the same band.
        and other.box.y0 >= slot.box.y1 - 2
        and other.box.x0 < slot.box.x1
        and other.box.x1 > slot.box.x0
        for other in here
    )


def _is_caption(slot: Slot, presentation: Any) -> bool:
    """A source line or a figure caption, which a deck sets small on purpose."""
    head = slot.head.strip().lower()
    if any(head.startswith(marker) for marker in _CAPTION_MARKERS):
        return True
    return _is_footer(slot, presentation)


def drift_findings(pptx_path: Path, spans: Sequence[Span] | None) -> list[Finding]:
    """Boxes a deck repeats that the renderer set at different sizes.

    The group is the slot: same declared size, same width and height, anywhere in
    the deck. Nothing here compares two slots that were drawn differently -- a
    heading is meant to be larger than its body -- only copies of one slot, which a
    reader expects to agree and which a template guarantees will not when the copy
    going into them varies in length.
    """
    if not spans:
        return []
    groups: dict[tuple[float | None, float, float], list[Slot]] = {}
    for slot in slots(pptx_path, spans):
        if slot.rendered_pt is None or slot.chars < _SLOT_CHARS:
            continue
        groups.setdefault((slot.declared_pt, *slot.shape), []).append(slot)
    findings: list[Finding] = []
    for (declared, width, height), members in sorted(groups.items(), key=lambda item: str(item[0])):
        if len(members) < 2:
            continue
        # The slot's own size, not the popular one. Autofit only ever shrinks, so the
        # largest setting any copy of the slot came out at is the size the slot was drawn
        # at, and every smaller one is a box that could not hold what went into it. Taking
        # the size most characters were set at instead inverted the finding: on a page of
        # six cards it called the four shrunk bodies the house style and the two headings
        # at the slot's real size the outliers.
        house = declared if declared is not None else max(slot.rendered_pt for slot in members)
        off = [slot for slot in members if slot.rendered_pt * DRIFT_RATIO < house]
        for page in sorted({slot.page for slot in off}):
            here = [slot for slot in off if slot.page == page]
            sizes = ", ".join(f"{slot.rendered_pt:g}pt ('{slot.head}')" for slot in here)
            findings.append(
                Finding(
                    kind="type_drift",
                    severity=Severity.WARNING,
                    page=page,
                    message=(
                        f"this deck repeats a {width:g}x{height:g}in text box {len(members)} times at {house:g}pt, and "
                        f"on this page {len(here)} of them came out smaller: {sizes}. Each box shrank its own copy to "
                        f"fit, so one slot is showing several sizes -- give these boxes the height {house:g}pt needs, "
                        f"or even out how much they hold. A reader reads a page against the page before it"
                    ),
                    detail={
                        "page": page,
                        "slot_in": [width, height],
                        "boxes": _boxes(here),
                        "declared_pt": declared,
                        "house_pt": house,
                        "sizes_pt": [slot.rendered_pt for slot in here],
                        "repeats": len(members),
                    },
                )
            )
    return findings


# Why the pair readings below are held to a tie rather than to any inequality, measured
# over the ten bundled templates writing their own copy into their own boxes: four pages
# set the line under the title *deliberately* larger -- a 54pt "662+" statistic, a 72pt
# section numeral, a 40pt "04", a 38.8pt pull-quote over a 28pt page title -- and every
# one of them is a design idiom rather than a defect. Nobody chooses "exactly the size of
# the thing above it" on purpose, and on a cloned page nobody can: the title states no
# size and resolves through the master. The audit that named this says the same thing from
# the other end -- the old decks carried flat hierarchies but never an inverted one.
HEADING_TIE = 1.02
# And how much of the title's own width the line under it has to span before the two are
# two heading rows rather than a heading and a label beside it. A template's contents page
# stacks a 259pt-wide English gloss under an 846pt-wide two-character title at one size, and
# read as a tie that is a false positive: the render shows one bilingual heading pair, not
# two headings.
# The four pages this does report are all the same full-width box repeated -- 11.88in over
# 11.88in, which is a template that drew two title rows.
HEADING_ROW_WIDTH = 0.8


def outranked_findings(pptx_path: Path, spans: Sequence[Span] | None, template: Path | None = None) -> list[Finding]:
    """Pages whose title came out at the same size as the line under it.

    The state a reader describes as two competing headings, and on the page that named
    it the caption was the longer of the two, so the caption won the eye. What makes it
    a measurement rather than taste is that both numbers are in the render: one
    delivered page has its title and its chart caption at 28.01pt each, the title 37
    characters and the caption 57.

    Read off the render for the same reason everything else here is. The file says the
    caption is 28pt and says nothing at all about the title, and on the page before it
    the same 28pt caption came back at 20.6pt because its copy was longer -- so the file
    reports a tie the renderer had already resolved, and would miss the reverse.

    Three things have to hold together, and the message states all three, so each one
    is a guard rather than a filter: the two are at one size, the second is *under* the
    title, and it is the *longer* of the two. Under and longer are the mechanism -- at
    one size the eye goes to the longer line, and a line above the title or shorter than
    it does not take the heading off it. `_heading_rows` answers the role and not the
    relation: its named-placeholder branch deliberately returns a SUBTITLE wherever the
    template put it, a kicker set above the title included.

    `template` decides what the finding *asks for*, and it is the difference between
    right and wrong advice rather than a filter. Where the box carrying the stated size
    sits at one of the template's own coordinates, that size is the template's: the
    prototype already tied, cloning it inherited the tie, and telling the author to
    change the number is telling it to abandon the template. Where the author drew the
    box, the two numbers are its own and cost nothing to separate.
    """
    if not spans:
        return []
    presentation = open_deck(pptx_path)
    measured = {_where(slot.page, slot.box): slot for slot in slots(pptx_path, spans)}
    theirs = _template_boxes(template)
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        rows = _heading_rows(slide, presentation)
        title, under = rows.get("title"), rows.get("subtitle")
        if title is None or under is None:
            continue
        above = measured.get(_where(number, shape_rect_pt(title)))
        below = measured.get(_where(number, shape_rect_pt(under)))
        if above is None or below is None or above.rendered_pt is None or below.rendered_pt is None:
            continue
        # A heading is set above the size the deck calls body. Without this the reading
        # answered on a page that has no title at all: an author that stopped cloning
        # and drew its own page left `_heading_rows` to infer a title from the topmost
        # line in the top third, which on that page was a 12pt chart footnote sitting
        # 0.04in above the 12pt caption under it. Two 12pt lines are not a hierarchy.
        if above.rendered_pt <= BODY_PT:
            continue
        title_box, under_box = shape_rect_pt(title), shape_rect_pt(under)
        # Under the title in the sense `_heading_rows` itself uses wherever it reads
        # geometry -- `shape.top > title.top`. Its first branch reads the template's own
        # naming instead, and answers with a SUBTITLE placeholder wherever the template
        # put it, which is on purpose: a kicker set above the title is still the row the
        # template called its subtitle. It is not the line under it, though, and a kicker
        # is where a tie is least likely to be a defect. Without this a full-width
        # two-character "Q3" over a title read as the line under it, and the message said
        # so while its own numbers said the opposite.
        if under_box.y0 <= title_box.y0:
            continue
        if under_box.width < HEADING_ROW_WIDTH * title_box.width:
            continue
        if not above.rendered_pt <= below.rendered_pt <= above.rendered_pt * HEADING_TIE:
            continue
        # And the longer of the two, which is the rest of the mechanism rather than a
        # detail of the wording: at one size the eye goes to the longer line, so a title
        # that is itself the longer line has not been outranked by anything. Two pages of
        # the evidence tree tie with an 83-character title over a 51-character caption.
        if below.chars <= above.chars:
            continue
        stated = _declared_pt(under)
        inherited = None if _declared_pt(title) is not None else inherited_size(title, page_design(presentation, slide))
        cloned = _matches(_inches(under_box), theirs)
        findings.append(_outranked(number, above, below, stated, inherited, cloned))
    return findings


def _outranked(
    page: int, above: Slot, below: Slot, stated: float | None, inherited: float | None, cloned: bool
) -> Finding:
    size = above.rendered_pt or 0
    if inherited is not None:
        seen = (
            f"The title box states no size of its own -- it resolves to {inherited:g}pt through the layout and "
            f"the master, which is why nothing in your program shows you the number the caption matched"
        )
    else:
        seen = f"Both sizes are stated on the page: the title at {_declared_pt_text(above)} and the caption below it"
    if cloned:
        fix = (
            "The caption box is the template's own, and so is the size in it -- the prototype ties too, so this "
            "came with the page rather than from your program. Set the caption explicitly a step down "
            "(`size=LABEL_PT`, or `BODY_PT`), or shorten it until it reads as a caption; do not touch the title"
        )
    else:
        fix = (
            "You drew this box and chose this size, so step it down: `size=BODY_PT` for a line under a title, "
            f"or `size=LABEL_PT`. Anything below {size:g}pt separates them and nothing on the page has to move"
        )
    return Finding(
        kind="outranked_title",
        severity=Severity.WARNING,
        page=page,
        message=(
            f"this page's title and the line under it both came out at {size:g}pt, and the line under it is the "
            f"longer of the two ({below.chars} characters against {above.chars}) -- so a reader meets the caption "
            f"as the heading: '{above.head}' over '{below.head}'. {seen}. {fix}"
        ),
        detail={
            "page": page,
            "title_pt": above.rendered_pt,
            "under_pt": below.rendered_pt,
            "title_chars": above.chars,
            "under_chars": below.chars,
            "stated_pt": stated,
            "inherited_pt": inherited,
            "under_is_the_templates_box": cloned,
        },
    )


def _declared_pt_text(slot: Slot) -> str:
    return f"{slot.declared_pt:g}pt" if slot.declared_pt is not None else "no size of its own"


# How far apart two members of one repeating row may render before the row reads as
# uneven. Calibrated over the ten bundled templates writing their own copy into their own
# boxes: 332 repeating rows carry a render, and their spread is 1.000 at the median, 1.000
# at the 95th and 1.045 at the 99th -- a designer filling a row keeps it even, so almost
# any gap at all is the author's. The two rows over this line are `green_aurora`'s pages
# 10 and 11, and both were rendered and looked at: four card headings at 16.2 / 16.2 /
# 12.2 / 13.3pt and five at 18 / 18 / 18 / 15.1 / 14.2pt, visibly uneven on the page.
# They are true positives in a shipped template, not noise to tune away.
# (327 before a level was identified by where it sits in the unit as well as by its shape;
# the five it gained are levels that had been merged with another of the same shape, and
# every quantile and both reporting rows came back the same.)
ROW_DRIFT = 1.10
# A row is two boxes at least. Everything narrower than that is not a row.
_ROW_MEMBERS = 2
# One slot of a repeating unit: where it sits in the unit, and the box and stated size it
# was drawn with. The place comes first because it is what says *which* slot -- the shape
# alone does not, and two levels of one card are routinely drawn the same.
Level = tuple[int, float, float, float | None]


def row_findings(pptx_path: Path, spans: Sequence[Span] | None) -> list[Finding]:
    """Repeating rows whose members came back at different sizes.

    The finding is the row, not the box. Every member of the row is behaving correctly:
    each holds more copy than its height can show at the stated size, each shrinks its
    own type to fit, and each arrives at its own answer -- so a per-box reading can look
    at all of them and see nothing. What a reader sees is one card at 17pt beside four at
    20pt.

    Measured across the ten bundled templates, 270 of 402 repeating rows have every
    member set to shrink and only 7 mix shrinking with non-shrinking, so *which* members
    shrink says almost nothing at row level. The discriminating fact is by how much, and
    only the render knows that: autofit has happened by the time a span is painted.

    Siblings come from `units`, which is the same grouping `fill` writes through, so a
    row named here is a row the author addressed as one. The level within it is one slot
    of the unit -- where it sits in the unit, at what size, drawn at what stated size --
    which is `drift_findings`' group key narrowed from the whole deck to one row of one
    page, and that narrowing is the point: a row that renders uniformly small is a row a
    reader has no complaint about.
    """
    if not spans:
        return []
    presentation = open_deck(pptx_path)
    measured = {_where(slot.page, slot.box): slot for slot in slots(pptx_path, spans)}
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        for row, level, here in _rows(slide, number, measured):
            sizes = sorted(slot.rendered_pt or 0 for slot in here)
            if not sizes[0] or sizes[-1] / sizes[0] < ROW_DRIFT:
                continue
            findings.append(_uneven(number, row, level, here))
    return findings


def _rows(slide: Any, page: int, measured: dict) -> list[tuple[int, Level, list[Slot]]]:
    """Every level of every repeating unit on this page, with the size each member came out at.

    A level is one slot of the unit, and where it sits in the unit is part of saying
    which slot: two levels of one unit can be drawn to the same size and stated at the
    same number, and a card whose heading and body are both 3x0.6in at 20pt is the
    ordinary case rather than a corner of one. Keyed on shape alone, all four boxes of a
    two-card run merged into one level and reported a 1.429 spread over a run where
    neither row drifts at all -- the headings agreeing with each other at 20pt and the
    bodies agreeing with each other at 14pt is the deck behaving.

    Where it sits is its rank down the page among the unit's slots -- `_slots_of` decides
    which frames those are and `_place` reads the box off the page and orders on it, top
    to bottom and then left to right. The rank is fixed over the slots and only then are
    the frames this reading cannot use dropped, which is the difference between a
    structural position and a position in the copy that happens to be there: an option
    one unit left unspoken must not shift the ordinal of every slot under it in that unit
    alone. Ranking the frames that hold words did exactly that -- two cards with an
    optional label over an identical body slot, the label supplied on the second card
    only, put the two bodies at ranks 1 and 2 and stopped comparing them, so a row
    running 20pt against 14pt reported nothing.

    Not `_reading_order`, which is how `fill` addresses the slots of a unit and is right
    for writing into one and wrong as an identity. It bands shapes into rows so a number
    beside a heading is met before it; the band grows as shapes join it, so its membership
    turns on the order they were scanned in, and that order comes off `shape.top`, which a
    grouped shape states in its own group's coordinate space. Two units of one run
    therefore number their slots differently. Measured on the templates: one deck's
    three-card run stores the card number first in one unit and third in the other two,
    and `_reading_order` puts the heading second in that unit and first in the others -- a
    3-card row read as 2 cards and 1.
    """
    found = []
    for run in units(slide):
        levels: dict[Level, list[Slot]] = {}
        for unit in run:
            for position, shape in enumerate(_slots_of(unit, _slot_shapes(run)), start=1):
                # Everything from here down drops a frame from the row without moving the
                # rank of anything under it, which is why the rank is taken first.
                if not (shape.text_frame.text or "").strip():
                    continue
                slot = measured.get(_where(page, shape_rect_pt(shape)))
                if slot is None or slot.rendered_pt is None or slot.chars < _SLOT_CHARS:
                    continue
                stated = _declared_pt(shape)
                # Autofit only ever shrinks, so a member reading larger than the size its
                # own runs state cannot have got there from this box: it is a span from
                # something stacked over it that `slots` had nowhere tighter to put. One
                # delivered page's 14pt body box read 30pt off the number tile beside it.
                if stated is not None and slot.rendered_pt > stated:
                    continue
                levels.setdefault((position, *_drawn(shape), stated), []).append(slot)
        for level, members in levels.items():
            if len(members) >= _ROW_MEMBERS:
                found.append((len(run), level, members))
    return found


def _slot_shapes(run: Sequence[Any]) -> set[tuple[float, float]]:
    """The drawn shapes this run puts copy in, anywhere across its units.

    Which frames of a unit count as slots, decided by the run rather than by the unit,
    and this is the load-bearing definition of the reading above. A built page cannot ask
    the prototype: `adapt` empties every frame it was not told about, so an icon's
    container and a body slot the author had nothing to say about arrive at this function
    looking the same -- both empty, both still standing. The run is what can tell them
    apart. A shape that holds copy in some unit is a slot, and its emptiness in another
    unit is an option that unit declined; a shape empty in every unit of the run is
    furniture -- an icon container, a spacer, a rule -- and never had a position to hold.

    It is `fill`'s own rule read across the run instead of within one unit. `fill` calls a
    frame a spacer because the frames beside it in that unit hold text; a sibling unit
    that filled the same shape is strictly better evidence than that, and it is the
    evidence that decides the case `fill` cannot see, where the option is the *first*
    slot of the unit and its absence shifts everything after it.

    Stable across the units of one run because it is computed once for the run: every
    unit ranks against the same set, and whether a frame belongs to it turns on the run's
    copy rather than on which unit is being read.

    Measured all three ways over the ten bundled templates and the nine decks. Counting
    every text frame instead costs a true positive its whole row: `green_aurora`'s zigzag
    timeline draws the icon container above the heading in one of five units and below it
    in the other four, so the odd card falls out and page 11 reports four of five.
    Counting only the frames that hold copy is the false silence this replaces, and it is
    in the delivered corpus as well as in the synthetic case -- `v13_warm` page 5 has a
    4.45x0.7in slot whose two copies were never compared, because one unit left an
    earlier option unspoken. This definition keeps both: the templates come back
    unchanged at 332 rows with page 11 whole, and the decks gain that one pair.
    """
    return {
        _drawn(shape)
        for unit in run
        for shape in _all_shapes(unit.shapes)
        if getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip()
    }


def _slots_of(unit: Any, shapes: set[tuple[float, float]]) -> list[Any]:
    """One unit's slots, in the order a reader comes down the page."""
    return sorted(
        (
            shape
            for shape in _all_shapes(unit.shapes)
            if getattr(shape, "has_text_frame", False) and _drawn(shape) in shapes
        ),
        key=_place,
    )


def _drawn(shape: Any) -> tuple[float, float]:
    """A shape's own width and height in inches, rounded the way clones of a slot agree."""
    return (round((shape.width or 0) / EMU_PER_INCH, 2), round((shape.height or 0) / EMU_PER_INCH, 2))


def _uneven(page: int, row: int, level: Level, here: list[Slot]) -> Finding:
    position, width, height, stated = level
    biggest = max(slot.rendered_pt or 0 for slot in here)
    ordered = sorted(here, key=lambda slot: slot.rendered_pt or 0)
    sizes = ", ".join(f"{slot.rendered_pt:g}pt ('{slot.head}')" for slot in ordered)
    drawn = f"{stated:g}pt" if stated is not None else f"{biggest:g}pt, which no run states"
    return Finding(
        kind="row_type_drift",
        severity=Severity.WARNING,
        page=page,
        message=(
            f"this page repeats one unit {row} times and the {_ordinal(position)} slot down each unit -- the "
            f"{width:g}x{height:g}in box -- is drawn at {drawn}, "
            f"but its {len(here)} copies came out at different sizes: {sizes}. Each box shrank its own copy to fit, "
            f"so every one of them is behaving and the row is what reads wrong -- a reader meets "
            f"{ordered[0].rendered_pt:g}pt beside {biggest:g}pt. Even out how much the units hold, or set that slot "
            f"one size explicitly so they agree; giving one box more height fixes one card and leaves the row uneven"
        ),
        detail={
            "page": page,
            "slot_in": [width, height],
            "slot_at": position,
            "boxes": _boxes(here),
            "declared_pt": stated,
            "units": row,
            "members": len(here),
            "sizes_pt": [slot.rendered_pt for slot in ordered],
            "spread": round(biggest / (ordered[0].rendered_pt or 1), 3),
        },
    )


def _place(shape: Any) -> tuple[float, float]:
    """Where a shape sits on the page, for ranking the slots of one unit against each other.

    On the page and not as declared: a shape inside a group states its own numbers in the
    group's coordinate space, and two units of one run do not share that space.
    """
    box = shape_rect_pt(shape)
    return (round(box.y0, 1), round(box.x0, 1))


def _ordinal(position: int) -> str:
    if position % 100 not in (11, 12, 13) and position % 10 in (1, 2, 3):
        return f"{position}{('st', 'nd', 'rd')[position % 10 - 1]}"
    return f"{position}th"


def _boxes(here: Sequence[Slot]) -> list[list[float]]:
    """The boxes a type finding is about, in the coordinates `_where` keys them by.

    The one identity the row reading and the deck-wide one can both state, and `quiet`
    needs one: a level here is a slot inside a unit and `drift_findings`' group is a
    shape repeated anywhere in the deck, so neither side's own key means anything to the
    other. Sharing `[width, height]` instead let a row on one level of a page silence
    `type_drift` on a different level of the same page that happened to be drawn the
    same size, which is the reading it was supposed to leave standing.
    """
    return [list(_where(slot.page, slot.box)[1:]) for slot in sorted(here, key=lambda slot: (slot.box.y0, slot.box.x0))]


def _where(page: int, box: Rect) -> tuple[int, float, float, float, float]:
    """One box's place on one page, as the key `slots` and a shape walk both arrive at.

    `slots` hands back the box and not the shape it came from, and `shape_rect_pt` is
    what put it there, so calling it again on the same shape lands on the same numbers.
    Keyed rather than re-derived so the two readings above share one attribution of
    spans to boxes with `type_floor` and `type_drift` instead of writing a fourth.
    """
    return (page, round(box.x0, 1), round(box.y0, 1), round(box.x1, 1), round(box.y1, 1))


# The two steps of `ppt_layout`'s ramp this measurement is about: the size the deck
# calls body, and the one step the ramp puts between it and the floor. Copy may be set
# at either, and at 15pt it was set at neither.
#
# Stated here rather than imported, and not for the usual reason. `ppt_layout` is not a
# module the engine can import: it exists as the *text* of a module, written beside the
# author's script and imported by that script alone. So the two copies are pinned
# against each other by a test that writes the helper out and reads its own constants
# back, which is the only place the agreement can be checked at all.
BODY_PT = 16.0
LABEL_PT = 14.0


# Raising a size costs room here too, so this reports and never refuses -- invariant 3,
# and the same oscillation `_FIX` is written against. What it can say that `_FIX` cannot
# is where the size should have come from: nobody picks 15pt for a reason, and the call
# that answers the question properly already exists.
_SCALE_FIX = (
    "Name the ramp: `size=BODY_PT` for copy, or `size=LABEL_PT` -- the one step it puts between the body "
    "size and the floor. Where the copy has to fit a box, ask "
    "`the_largest_step_this_copy_takes(text, box, font=F)` for the step rather than settling on an integer, "
    "and give the copy the room the larger size needs"
)


def scale_findings(pptx_path: Path, template: Path | None = None) -> list[Finding]:
    """Pages whose copy is set between the floor and `BODY_PT`, off the ramp.

    A page-level reading of what a per-run floor cannot see. The floor judges each run
    against the tier it belongs to -- 8pt in the footer band, 10.8pt for a caption or a
    short label, 14pt for copy -- and every one of those tiers is satisfied by a page
    whose body runs at 15pt. One live page had 20 of its 31 runs at 12pt, nothing at all
    at 16pt, and its bullets at 15pt; the floor reported one box on it, correctly, and
    the page still reads two steps small.

    What is reported is the copy and only the copy: text of at least `_COPY_CHARS`
    characters that is not a caption or a footer, which is the line this file already
    draws for its own floor tier and is reused rather than doubled. The wider claim --
    "this page's dominant size is under `BODY_PT`" -- was measured over 45 generated
    pages and fires on 31 of them, because a chart's month labels and a card's kicker
    are `KICKER_PT` on purpose and outnumber the prose on any page carrying a figure.
    That number is still worth saying, so it goes in the message as evidence rather
    than in the trigger. This trigger fires on 11 of the same 45, and 10 of the 11 are
    pages `type_floor` says nothing at all about.

    And only type the author chose. Inside a user's template the copy is the template's:
    the ten bundled templates set 335 of their 356 copy blocks under 16pt, 269 of them at
    12pt, so a check held to our ramp would report every page of every templated deck and
    ask for a fix that means abandoning the template -- the dead end of invariant 6. A
    cloned page keeps its prototype's positions exactly (`measure.adherence`), so a box
    sitting where the template puts one is the template's and is left alone. `template`
    is the file the user handed over, example pages included, the same one
    `template_adherence` and `prototype_kept` compare against; without it every box is
    the author's, which is what a deck built from a blank presentation is.
    """
    presentation = open_deck(pptx_path)
    inherited = _template_boxes(template)
    off: dict[int, list[Slot]] = {}
    at_body: dict[int, bool] = {}
    for slot in slots(pptx_path, ()):
        if slot.declared_pt is None or slot.chars < _COPY_CHARS or _is_caption(slot, presentation):
            continue
        # Whether this page has any body-size copy at all, which is the difference
        # between one block set small and a page with no body size on it.
        at_body[slot.page] = at_body.get(slot.page, False) or slot.declared_pt >= BODY_PT
        # At or over the floor, under the body size, and not the step the ramp puts
        # between them. Under the floor is `type_floor`'s, which says "bring it up"
        # about the same box -- two findings on one box is a finding an author learns
        # to skip.
        if not BODY_FLOOR_PT <= slot.declared_pt < BODY_PT or slot.declared_pt == LABEL_PT:
            continue
        if _matches(_inches(slot.box), inherited):
            continue
        off.setdefault(slot.page, []).append(slot)
    # The page's own scale, off the census rather than measured a second way: the size
    # most of its characters are set at, tables and short labels included.
    scale = {page.page: page.declared_pt for page in census(pptx_path, None)}
    findings: list[Finding] = []
    for page, blocks in sorted(off.items()):
        blocks.sort(key=lambda slot: slot.declared_pt or 0.0)
        sizes = sorted({slot.declared_pt for slot in blocks if slot.declared_pt is not None})
        named = ", ".join(f"{slot.declared_pt:g}pt over {slot.chars} characters ('{slot.head}')" for slot in blocks[:3])
        more = f", and {len(blocks) - 3} more" if len(blocks) > 3 else ""
        one = len(blocks) == 1
        dominant = scale.get(page)
        findings.append(
            Finding(
                kind="type_scale",
                severity=Severity.WARNING,
                page=page,
                message=(
                    (
                        f"this page's copy is set at {sizes[0]:g}pt, which is not a step of the ramp: {named}. It "
                        f"clears the {BODY_FLOOR_PT:g}pt floor, so no other check reports it, and it is still under "
                        f"BODY_PT ({BODY_PT:g}pt)"
                        if one
                        else f"{len(blocks)} blocks of copy on this page are set at "
                        f"{' and '.join(f'{size:g}pt' for size in sizes)}, which the ramp does not have: "
                        f"{named}{more}. They clear the {BODY_FLOOR_PT:g}pt floor, so no other check reports them, "
                        f"and they are still under BODY_PT ({BODY_PT:g}pt)"
                    )
                    + (
                        f" -- no copy on this page reaches {BODY_PT:g}pt at all"
                        if not at_body.get(page)
                        else f", though the page does set other copy at {BODY_PT:g}pt or over"
                    )
                    + (
                        ""
                        if dominant is None
                        else f", and {dominant:g}pt is what most of its characters are set at"
                        if dominant in sizes
                        else f", and the page's own type mostly runs at {dominant:g}pt"
                    )
                    + f". {_SCALE_FIX}"
                ),
                detail={
                    "page": page,
                    "body_pt": BODY_PT,
                    "label_pt": LABEL_PT,
                    "body_floor_pt": BODY_FLOOR_PT,
                    "sizes_pt": [slot.declared_pt for slot in blocks],
                    "chars": [slot.chars for slot in blocks],
                    "page_body_pt": dominant,
                    "reaches_body_pt": at_body.get(page, False),
                },
            )
        )
    return findings


def _template_boxes(template: Path | None) -> set[tuple[float, float, float, float]]:
    """Where the template puts a shape, in inches, over every page it ships.

    In page coordinates, which is why this does not call `adherence._pages`: that one
    reads a shape's own numbers, and a shape inside a group states them in the group's
    space. Both sides of the comparison have to be in one space or a template's grouped
    card matches nothing, and `slots` already hands its boxes back on the page.
    """
    if template is None or not Path(template).is_file():
        return set()
    try:
        presentation = open_deck(Path(template))
    except Exception:  # noqa: BLE001 -- an unreadable template is no signal, not a defect
        return set()
    return {
        _inches(shape_rect_pt(shape))
        for slide in presentation.slides
        for shape in iter_shapes(slide.shapes)
        if shape.left is not None
    }


def _inches(box: Rect) -> tuple[float, float, float, float]:
    """A box in inches, rounded the way `adherence` compares two of them."""
    return (round(box.x0 / 72, 2), round(box.y0 / 72, 2), round(box.x1 / 72, 2), round(box.y1 / 72, 2))


def _head(text: str, limit: int = 20) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
