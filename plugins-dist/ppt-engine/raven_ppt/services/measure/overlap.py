"""A shape covered by a shape, which no other check can see.

`word_collision` reads the render and reports a word painted over a word, which is
the loud half of overlapping. This is the quiet half: content that is not collided
with but simply *behind* something. A figure under an opaque card is invisible --
nothing is painted over anything, no word touches another word, and the page cites
evidence it does not show. One deck hid a paper's architecture figure under three
white panels and every check came back green.

The measurement is z-order and area: a shape whose box is mostly covered by shapes
drawn *after* it that are painted solid. Order is what makes it decidable, because
copy inside a panel is the same two boxes in the same place and is the whole idea of
a card -- the panel is under the text, and a panel over the text is the defect.

What was tried here and rejected: reporting two text boxes that overlap, off the
file. It reads as the same check and it is not sound. A page's text box is wider
than the copy in it, so a full-width title box overlaps the small box holding a
corner page number on every well-made page in the sample -- 8 of the 9 findings it
produced on a good deck were exactly that, and the two real collisions it found
were already blocking as `word_collision`, where the words themselves are measured
rather than the boxes they were put in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import EMU_PER_INCH, PICTURE, Rect, is_panel, iter_shapes, open_deck, page_box

# This much of a shape hidden under later shapes and it is not on the page any more.
# A figure half behind a panel is a design; a figure three fifths behind one is a
# figure nobody sees.
COVERED = 0.6
# A shape this big is the page's ground -- a full-bleed photograph, a tinted half.
# A ground cannot be "hidden", so it is never the subject of this check; it can
# certainly hide something, so it stays in the z-order as one of the shapes that
# cover. A page whose background was drawn last hides everything under it, which is
# the one case an exclusion on size would have made invisible.
IS_GROUND = 0.55
# Under this a shape is a mark: a bullet, a rule, a step number's bubble.
MIN_AREA_IN = 0.05


@dataclass(frozen=True)
class Piece:
    """One shape on a page, as overlap sees it."""

    index: int
    kind: str
    """`text`, `picture`, `table`, `panel` or `shape`."""
    box: Rect
    head: str
    element: Any
    opaque: bool

    ground: bool = False

    @property
    def carries_content(self) -> bool:
        return self.kind in ("text", "picture", "table") and not self.ground


def overlap_findings(pptx_path: Path) -> list[Finding]:
    """Content painted over by something drawn after it."""
    presentation = open_deck(pptx_path)
    page_area = (presentation.slide_width / EMU_PER_INCH) * (presentation.slide_height / EMU_PER_INCH)
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        findings.extend(_covered(_pieces(slide, page_area), number))
    return findings


def _pieces(slide, page_area: float) -> list[Piece]:
    found: list[Piece] = []
    for index, shape in enumerate(iter_shapes(slide.shapes), start=1):
        # Through the groups above it: a child's own numbers are in the group's
        # coordinate space, so reading them raw put a template's six agenda numbers
        # at one point and had five of them "completely hidden" under the sixth.
        box = page_box(shape)
        if box is None or not shape.width or not shape.height:
            continue
        if box.area < MIN_AREA_IN:
            continue
        text = shape.text_frame.text.strip() if getattr(shape, "has_text_frame", False) else ""
        picture = getattr(shape, "shape_type", None) == PICTURE
        kind = (
            "text"
            if text
            else "picture"
            if picture
            else "table"
            if getattr(shape, "has_table", False)
            else "panel"
            if is_panel(shape)
            else "shape"
        )
        found.append(
            Piece(
                index=index,
                kind=kind,
                box=box,
                head=_head(text),
                element=shape._element,
                opaque=picture or is_panel(shape),
                ground=box.area > IS_GROUND * page_area,
            )
        )
    return found


def _covered(pieces: list[Piece], page: int) -> list[Finding]:
    """Content painted over by shapes drawn after it."""
    findings: list[Finding] = []
    for index, piece in enumerate(pieces):
        if not piece.carries_content:
            continue
        over = [later for later in pieces[index + 1 :] if later.opaque and not _related(piece, later)]
        hidden = _covered_share(piece.box, [later.box for later in over])
        if hidden < COVERED:
            continue
        findings.append(
            Finding(
                kind="covered_shape",
                severity=Severity.WARNING,
                page=page,
                message=(
                    f"the {piece.kind} at shape {piece.index}"
                    + (f" ('{piece.head}')" if piece.head else "")
                    + f" is {hidden:.0%} hidden under shape(s) {', '.join(str(later.index) for later in over)}, "
                    "which are drawn after it and painted solid. Nothing on this page collides -- the content is "
                    "simply behind something. Move it into the clear, or take away what is over it"
                ),
                detail={
                    "page": page,
                    "shape": piece.index,
                    "under": [later.index for later in over],
                    "hidden": round(hidden, 2),
                },
            )
        )
    return findings


def _covered_share(box: Rect, over: list[Rect]) -> float:
    """How much of `box` the shapes above it cover, counting no strip twice.

    Summing the intersections double-counts wherever two of them overlap each other,
    which on a page of cards is most of them, so the covered area is measured on a
    grid of the box's own edges -- exact for axis-aligned rectangles and cheap at the
    handful of shapes a page has.
    """
    if not over or not box.area:
        return 0.0
    xs = sorted({box.x0, box.x1, *(value for rect in over for value in (rect.x0, rect.x1) if box.x0 < value < box.x1)})
    ys = sorted({box.y0, box.y1, *(value for rect in over for value in (rect.y0, rect.y1) if box.y0 < value < box.y1)})
    covered = 0.0
    for left, right in zip(xs, xs[1:]):
        for top, bottom in zip(ys, ys[1:]):
            cell = Rect(left, top, right, bottom)
            if any(_intersection(cell, rect) > 0.9 * cell.area for rect in over):
                covered += cell.area
    return covered / box.area


def _intersection(one: Rect, two: Rect) -> float:
    return max(0.0, min(one.x1, two.x1) - max(one.x0, two.x0)) * max(0.0, min(one.y1, two.y1) - max(one.y0, two.y0))


def _related(one: Piece, two: Piece) -> bool:
    """A group and something inside it, which is not two shapes in one place."""
    return one.element in two.element.iterancestors() or two.element in one.element.iterancestors()


def _head(text: str, limit: int = 24) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
