"""Edges that were meant to line up and did not.

Forty-three checks measure a built deck and every one of them asks whether
something is *inside* something else -- inside its box, inside its card, inside
the canvas, off the type floor. None of them asks whether two things that were
meant to share an edge actually share it, and a reader sees that first: a
delivered 12-page proposal cleared every blocking gate while three of its pages
set the last block of a column one seventh of an inch right of the blocks above
it, in nine places.

**Why this cannot be measured on the file, and cannot be measured on the render
either.** The author's program wrote `add_text(slide, 1.02, ...)` for that block
and `1.02` for the ones above it -- the declared geometry says they are flush,
and it is not wrong about where the boxes are. What it cannot see is the *left
inset* of the text frame: 199 boxes in that deck inherit a 0.0in inset from the
template and 11 carry python-pptx's own 0.1in default, so the same declared x
puts the copy 7.1pt apart on paper. The render, for its part, shows the 7.1pt
but not whether anyone meant it -- a block indented on purpose looks identical.

So the two ground truths answer different halves and this module needs both
(design doc D15 keeps the file half honest; the shape rectangles here are page
coordinates, group transform applied):

* the .pptx supplies the **intent** -- two boxes declared at the same x were
  meant to be flush, and no reading of the render can establish that;
* the render supplies the **truth** -- where the copy landed once the renderer
  had applied the inset, the alignment and the font.

A finding is exactly the disagreement: declared equal, rendered apart. That
also draws the line the check lives or dies on. An author who writes 1.02 and
1.12 has said something, however clumsily, and this stays quiet; an author who
writes 1.02 twice and gets two different edges has not, and that is a slip. The
alternative -- deciding from the render alone which indents look deliberate --
is the road design doc D17 records: three exemptions, each one an admission
that the measurement could not tell what it was looking at.

Every finding here is a WARNING (design doc D2, and D17 in advance). Ragged
edges are worth a rebuild and never worth refusing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    Rect,
    has_text,
    is_filled,
    iter_shapes,
    open_deck,
    pages,
    shape_rect_pt,
)
from raven_ppt.services.measure.rendered import (
    CARD_MIN_HEIGHT_PT,
    CARD_MIN_WIDTH_PT,
    SAME_LINE_PT,
)
from raven_ppt.services.measure.words import WordBox, by_page, rect

PT_PER_INCH = 72.0

# Two boxes are declared flush when their declared left edges are this close. The
# unit is a rounding artefact, not a design decision: the four cards of the page
# this was found on were laid out by the same expression and still land 0.02 to
# 0.25pt apart in EMU. Nobody offsets a box by a point on purpose.
FLUSH_SLOP_PT = 1.0

# A rendered word belongs to the box that holds this much of it, and to the
# smallest such box -- the same share `rendered.box_overflows` claims words with,
# and the smallest because a card-wide backing box would otherwise swallow the
# copy of every box drawn over it.
CLAIM_SHARE = 0.5

# How far apart two blocks can be and still read as one run. Both gates apply.
#
# The ratio is against the run's own rhythm, which is how `rendered.
# unseparated_blocks` already decides where a block ends: on the page this check
# was built for, the bullets of a card sit 24.5pt apart and the card's footer 66pt
# below the last of them, so the footer is a separate thing and its edge answers to
# nothing above it. The ceiling covers a run of two, where there is no rhythm to
# measure yet: 3.5 line-heights is the 47pt gap between the rows of that deck's
# hand-drawn table (15.6pt copy) with room to spare, and short of the 66pt above.
RUN_RATIO = 1.5
RUN_CEILING = 3.5

# What the glyph itself is allowed to contribute, as a share of the line's own
# height. A word's rendered bbox starts at its ink, not at its origin, so a line
# opening on a narrow figure begins further right than one opening on a wide
# letter, and the effect scales with type size: measured at 0.15 of the line
# height on 45pt tabular figures in this deck's font, against 0.003 on its 12pt
# CJK body. 0.20 clears the first with margin and still fires on the 7.1pt slip
# that produced this check, which is 0.46 of its own line's height.
BEARING_SHARE = 0.20
# And a floor for small type, where a share of the height is smaller than any
# offset anybody would notice.
DRIFT_FLOOR_PT = 2.0
# Above this the offset is an indent, and an indent is a decision -- a quarter
# inch is a deep one. The real cases all sit at 7.0-7.3pt; the nearest thing this
# spares on the two decks measured is 21.6pt, the gap between a page's content
# margin and its footer margin.
INDENT_PT = 0.25 * PT_PER_INCH

# When the file does not declare a paragraph's alignment, the render has to show
# that the line is set left rather than centred, and this is what "show" means:
# the room on its right exceeds the room on its left by a full line height.
# Deliberately strict. A centred line whose box happens to sit off-centre is
# indistinguishable from a left-set line at this range, and the cost of guessing
# wrong is a finding on a page that is fine.
LEFT_EVIDENCE = 1.0

# A shape drawn in the space a block left empty explains the block's indent: an
# accent bar or an icon at the block's left is why the copy moved in. It has to
# stop where the copy starts -- otherwise a table's own row band, which spans the
# whole row, would explain every row's indent, including the wrong ones.
GAP_COVER = 0.5
EDGE_SLOP_PT = 2.0

# Two containers are a row when their y-spans agree this closely and their
# x-spans do not touch at all.
ROW_SHARE = 0.8
# A panel this much of the canvas is the page's ground rather than a container on
# it, and its contents are the page's, not a card's.
GROUND_SHARE = 0.9
# A block belongs to the container that holds this much of its box. Not "its centre
# is inside": a badge half over a card's rim is content the card holds.
HELD_SHARE = 0.7
# How far apart the same element in two sibling containers may sit. 16 device
# pixels at 96dpi, which is where the reference rule this follows puts it: a row
# of cards is read as one object and the eye picks up a tail out of line before it
# reads any of them.
SIBLING_DRIFT_PT = 16 * PT_PER_INCH / 96

# One inherited inset offsets every block that carries it, so a page reports the
# offset rather than each block that has it; this caps how many distinct offsets
# one page can report.
DRIFTS_PER_PAGE = 2


@dataclass(frozen=True)
class Block:
    """One text frame's copy, as the file declares it and as the render set it."""

    box: Rect
    left: float
    right: float
    top: float
    height: float
    tops: tuple[float, ...]
    inset: float | None
    text: str

    @property
    def last_top(self) -> float:
        return self.tops[-1]


def alignment_findings(pptx_path: Path, words: Sequence[WordBox] | None) -> list[Finding]:
    """Both readings of a built deck's alignment, or nothing without a render."""
    if not words:
        return []
    return flush_drift(pptx_path, words) + sibling_drift(pptx_path, words)


def flush_drift(pptx_path: Path, words: Sequence[WordBox] | None, per_page: int = DRIFTS_PER_PAGE) -> list[Finding]:
    """Blocks the file declares flush that the render sets on different edges.

    Down a column, not across the page: the comparison is always against the block
    directly above, in the same run, declared at the same x. What comes back is one
    finding per offset per page rather than one per block, because the cause is one
    thing -- on the deck this was built from, a single inherited inset moved four
    blocks on one page by the same 7.1pt, and four findings would have the author
    fix it four times.
    """
    if not words:
        return []
    painted = by_page(words)
    findings: list[Finding] = []
    for number, slide in pages(pptx_path):
        on_page = painted.get(number, [])
        if not on_page:
            continue
        drawn, blocks = _read_page(slide, on_page)
        drifted: dict[int, list[tuple[Block, Block, float]]] = {}
        for above, below in _flush_pairs(blocks):
            drift = below.left - above.left
            tall = max(above.height, below.height)
            if abs(drift) <= max(DRIFT_FLOOR_PT, BEARING_SHARE * tall) or abs(drift) > INDENT_PT:
                continue
            if _decorated(drawn, above, below):
                continue
            drifted.setdefault(round(drift), []).append((above, below, drift))
        for offset in sorted(drifted, key=lambda value: -abs(value))[:per_page]:
            findings.append(_flush_finding(number, drifted[offset]))
    return findings


def sibling_drift(pptx_path: Path, words: Sequence[WordBox] | None, per_page: int = DRIFTS_PER_PAGE) -> list[Finding]:
    """The same element of side-by-side containers, set at different heights.

    A row of cards is read as one object, so the eye picks up a tail out of line
    before it reads any of the tails. What makes the comparison decidable is that
    the containers hold the same number of blocks: then the nth block of one is the
    nth block of the others and a difference in where it sits is a difference in
    nothing else. Cards holding different numbers of blocks are not compared at
    all -- there is no correspondence to compare, and inventing one (pairing by
    position from the bottom, say) is how a measurement starts reporting cards for
    having less to say.
    """
    if not words:
        return []
    painted = by_page(words)
    canvas = _canvas(pptx_path)
    findings: list[Finding] = []
    for number, slide in pages(pptx_path):
        on_page = painted.get(number, [])
        if not on_page:
            continue
        _drawn, blocks = _read_page(slide, on_page)
        reported = 0
        for row in _rows(slide, canvas):
            held = [sorted((b for b in blocks if _sits_in(panel, b.box)), key=lambda b: b.top) for panel in row]
            if len({len(stack) for stack in held}) != 1 or not held[0]:
                continue
            for index in range(len(held[0])):
                if reported >= per_page:
                    break
                spread = [stack[index] for stack in held]
                if max(b.top for b in spread) - min(b.top for b in spread) <= SIBLING_DRIFT_PT:
                    continue
                findings.append(_sibling_finding(number, index, len(held[0]), spread))
                reported += 1
    return findings


def _read_page(slide: Any, on_page: Sequence[WordBox]) -> tuple[list[Rect], list[Block]]:
    """(what is drawn on this page, what copy the render set and where)."""
    frames: list[tuple[Any, Rect]] = []
    drawn: list[Rect] = []
    for shape in iter_shapes(slide.shapes):
        box = shape_rect_pt(shape)
        if box.area <= 0:
            continue
        if has_text(shape):
            frames.append((shape, box))
        else:
            drawn.append(box)
    owned: dict[int, list[WordBox]] = {}
    for word in on_page:
        span = rect(word)
        holder = None
        for slot, (_shape, box) in enumerate(frames):
            if box.overlap(span) / max(span.area, 1e-6) >= CLAIM_SHARE and (
                holder is None or box.area < frames[holder][1].area
            ):
                holder = slot
        if holder is not None:
            owned.setdefault(holder, []).append(word)
    blocks = [block for slot in sorted(owned) if (block := _block(*frames[slot], owned[slot])) is not None]
    return drawn, blocks


def _block(shape: Any, box: Rect, words: Sequence[WordBox]) -> Block | None:
    """One text frame's copy as the render set it, or None when it cannot be read.

    Two shapes are unreadable here and both are file facts rather than judgements.
    A frame with wrapping off is placed by the renderer rather than by the file --
    LibreOffice re-centres an autofitting one, and this deck's 45pt figures come
    out 9pt left of the box they are declared in -- so its declared x states
    nothing about where its copy will start. And copy that is not set flush left
    has no left edge to compare: where a centred line begins is a fact about how
    long it is.
    """
    if shape.text_frame.word_wrap is False:
        return None
    lines = _lines(words)
    first = lines[0]
    left = min(word.x0 for word in first)
    right = max(word.x1 for word in first)
    height = max(word.y1 - word.y0 for word in first)
    if not _set_left(shape, box, left, right, height):
        return None
    inset = shape.text_frame.margin_left
    return Block(
        box=box,
        left=left,
        right=right,
        top=min(word.y0 for word in first),
        height=height,
        tops=tuple(min(word.y0 for word in line) for line in lines),
        inset=None if inset is None else float(inset) / EMU_PER_INCH,
        text="".join(word.text for word in first)[:28],
    )


def _lines(words: Sequence[WordBox]) -> list[list[WordBox]]:
    """The words grouped into the lines the renderer set them on."""
    lines: list[list[WordBox]] = []
    for word in sorted(words, key=lambda word: (word.y0, word.x0)):
        if lines and abs(word.y0 - lines[-1][0].y0) <= SAME_LINE_PT:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _set_left(shape: Any, box: Rect, left: float, right: float, height: float) -> bool:
    from pptx.enum.text import PP_ALIGN

    declared = {para.alignment for para in shape.text_frame.paragraphs if para.text.strip()}
    if declared - {PP_ALIGN.LEFT, None}:
        return False
    if declared == {None}:
        return (box.x1 - right) - (left - box.x0) >= LEFT_EVIDENCE * height
    return True


def _flush_pairs(blocks: Sequence[Block]) -> list[tuple[Block, Block]]:
    """Every block and the block above it the file declares it flush with."""
    pairs: list[tuple[Block, Block]] = []
    for column in _columns(blocks):
        rhythm = _rhythm(column)
        for above, below in zip(column, column[1:]):
            gap = below.top - above.last_top
            tall = max(above.height, below.height)
            if gap <= 0 or gap > RUN_CEILING * tall:
                continue
            if rhythm is not None and gap > RUN_RATIO * rhythm:
                continue
            pairs.append((above, below))
    return pairs


def _columns(blocks: Sequence[Block]) -> list[list[Block]]:
    """The blocks grouped by the left edge the file declares for them."""
    grouped: list[list[Block]] = []
    for block in sorted(blocks, key=lambda block: block.box.x0):
        if grouped and block.box.x0 - grouped[-1][-1].box.x0 <= FLUSH_SLOP_PT:
            grouped[-1].append(block)
        else:
            grouped.append([block])
    return [sorted(column, key=lambda block: block.top) for column in grouped]


def _rhythm(column: Sequence[Block]) -> float | None:
    """The distance this column's own lines keep, or None when there are too few."""
    tops = sorted(top for block in column for top in block.tops)
    gaps = [second - first for first, second in zip(tops, tops[1:]) if second - first > 1.0]
    return median(gaps) if len(gaps) >= 2 else None


def _decorated(drawn: Sequence[Rect], above: Block, below: Block) -> bool:
    """Whether something is drawn in the space the lower block left empty."""
    low, high = sorted((above.left, below.left))
    gap = Rect(low, below.top, high, below.top + below.height)
    over = Rect(above.left, above.top, above.right, above.top + above.height)
    if gap.area <= 0:
        return False
    return any(
        shape.x1 <= high + EDGE_SLOP_PT and shape.overlap(gap) >= GAP_COVER * gap.area and shape.overlap(over) <= 0
        for shape in drawn
    )


def _canvas(pptx_path: Path) -> float:
    deck = open_deck(pptx_path)
    return (float(deck.slide_width) / EMU_PER_INCH) * (float(deck.slide_height) / EMU_PER_INCH)


def _rows(slide: Any, canvas: float) -> list[list[Rect]]:
    """Containers standing side by side, left to right, two or more to a row."""
    panels: list[Rect] = []
    for shape in iter_shapes(slide.shapes):
        if has_text(shape) or not is_filled(shape):
            continue
        box = shape_rect_pt(shape)
        if box.width < CARD_MIN_WIDTH_PT or box.height < CARD_MIN_HEIGHT_PT:
            continue
        if box.area / (canvas * PT_PER_INCH * PT_PER_INCH) >= GROUND_SHARE:
            continue
        panels.append(box)
    rows: list[list[Rect]] = []
    for panel in sorted(panels, key=lambda box: box.x0):
        for row in rows:
            beside = row[-1]
            shared = min(panel.y1, beside.y1) - max(panel.y0, beside.y0)
            if min(panel.x1, beside.x1) - max(panel.x0, beside.x0) > 0:
                continue
            if shared > 0 and shared / max(panel.height, beside.height) >= ROW_SHARE:
                row.append(panel)
                break
        else:
            rows.append([panel])
    return [row for row in rows if len(row) >= 2]


def _sits_in(container: Rect, box: Rect) -> bool:
    return box.area > 0 and container.overlap(box) / box.area >= HELD_SHARE


def _flush_finding(page: int, drifted: Sequence[tuple[Block, Block, float]]) -> Finding:
    """One page's report of one offset, with the fix that closes it."""
    above, below, drift = drifted[0]
    side = "right" if drift > 0 else "left"
    back = "left" if drift > 0 else "right"
    others = len(drifted) - 1
    also = ""
    if others == 1:
        also = " 1 more block on this page sits the same amount out."
    elif others > 1:
        also = f" {others} more blocks on this page sit the same amount out."
    return Finding(
        kind="flush_drift",
        severity=Severity.WARNING,
        page=page,
        message=(
            f"in the render {below.text!r} starts {abs(drift):.1f}pt ({abs(drift) / PT_PER_INCH:.2f}in) to the "
            f"{side} of {above.text!r} directly above it, although the file puts both boxes at the same x "
            f"({below.box.x0 / PT_PER_INCH:.2f}in) -- which is why nothing that reads the file alone can see "
            f"it.{_cause(above, below, drift)} Move it {abs(drift) / PT_PER_INCH:.2f}in {back} so the column "
            f"reads flush.{also}"
        ),
        detail={
            "above": above.text,
            "below": below.text,
            "drift_pt": round(drift, 2),
            "declared_x_in": round(below.box.x0 / PT_PER_INCH, 3),
            "above_inset_in": above.inset,
            "below_inset_in": below.inset,
            "also": [pair[1].text for pair in drifted[1:5]],
        },
    )


def _cause(above: Block, below: Block, drift: float) -> str:
    """The left inset, when the file carries one that accounts for the offset."""
    if above.inset is None or below.inset is None:
        return ""
    difference = (below.inset - above.inset) * PT_PER_INCH
    if abs(difference - drift) > max(DRIFT_FLOOR_PT, BEARING_SHARE * below.height):
        return ""
    return (
        f" The two text frames carry different left insets ({below.inset:.2f}in against "
        f"{above.inset:.2f}in), which is the whole of the offset."
    )


def _sibling_finding(page: int, index: int, total: int, spread: Sequence[Block]) -> Finding:
    """Corresponding blocks of a row of containers, reported against each other."""
    highest = min(spread, key=lambda block: block.top)
    lowest = max(spread, key=lambda block: block.top)
    apart = lowest.top - highest.top
    tail = " It is the last block in each, which is the one a reader lines up on." if index == total - 1 else ""
    return Finding(
        kind="sibling_drift",
        severity=Severity.WARNING,
        page=page,
        message=(
            f"block {index + 1} of {total} does not share a height across the {len(spread)} containers standing "
            f"side by side here: {lowest.text!r} sits {apart:.1f}pt ({apart / PT_PER_INCH:.2f}in) below "
            f"{highest.text!r}.{tail} Raise it by that much, or give what is above it the same room in every "
            f"container, so the row reads as one band."
        ),
        detail={
            "block": index + 1,
            "of": total,
            "spread_pt": round(apart, 2),
            "highest": highest.text,
            "lowest": lowest.text,
            "tops_pt": [round(block.top, 2) for block in spread],
        },
    )
