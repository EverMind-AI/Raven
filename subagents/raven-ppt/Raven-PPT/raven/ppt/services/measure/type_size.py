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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    Rect,
    iter_shapes,
    iter_text_frames,
    open_deck,
    shape_rect_pt,
)

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
# So this reports and never refuses, and it goes to the design pass, which is the
# one allowed to find the room.
_FIX = (
    "Bring it up, and give the copy the room the larger size needs rather than letting it overflow: "
    "fewer bullets, a wider column, or the page split. Do not shrink it back to fit"
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
                audience=Audience.DESIGNER,
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
    for slot in slots(pptx_path, spans):
        if slot.rendered_pt is None or slot.chars < _SLOT_CHARS:
            continue
        if _is_footer(slot, presentation):
            floor = FOOTER_FLOOR_PT
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
                audience=Audience.DESIGNER,
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
                    audience=Audience.DESIGNER,
                    message=(
                        f"this deck repeats a {width:g}x{height:g}in text box {len(members)} times at {house:g}pt, and "
                        f"on this page {len(here)} of them came out smaller: {sizes}. Each box shrank its own copy to "
                        f"fit, so one slot is showing several sizes -- give these boxes the height {house:g}pt needs, "
                        f"or even out how much they hold. A reader reads a page against the page before it"
                    ),
                    detail={
                        "page": page,
                        "slot_in": [width, height],
                        "declared_pt": declared,
                        "house_pt": house,
                        "sizes_pt": [slot.rendered_pt for slot in here],
                        "repeats": len(members),
                    },
                )
            )
    return findings


def _head(text: str, limit: int = 20) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
