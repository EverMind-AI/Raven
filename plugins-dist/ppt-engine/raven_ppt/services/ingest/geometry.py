"""Rectangle arithmetic on a source page, shared by every extractor.

Regions arrive from four unrelated readers -- image placements, drawing
clusters, table finders, text blocks -- and the questions asked of them are
always the same three: what do these cover together, do they sit over the same
columns, and is one of them inside another. Keeping the answers in one place is
what lets a caption matcher and a fragment check agree about the same page.

Everything is in PDF points with the origin top-left, and everything returns a
``fitz.Rect`` so intersections stay exact; tuples appear only at the contract
boundary, via :func:`as_tuple`.
"""

from __future__ import annotations

from raven_ppt.contracts.sources import Rect


def rect(box):
    """A ``fitz.Rect`` from anything four-valued."""
    import fitz

    return box if isinstance(box, fitz.Rect) else fitz.Rect(*box)


def union(boxes):
    """The one region covering all of ``boxes``, or None when there are none."""
    boxes = [rect(box) for box in boxes if box is not None]
    if not boxes:
        return None
    merged = rect(boxes[0])
    for box in boxes[1:]:
        merged |= box
    return merged


def as_tuple(box) -> Rect:
    """The contract shape: four floats, rounded to hundredths of a point."""
    x0, y0, x1, y1 = rect(box)
    return (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))


def horizontal_overlap(left, right) -> float:
    """Shared width as a fraction of the narrower box -- "same columns?"."""
    overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    narrower = min(left.width, right.width)
    return overlap / narrower if narrower else 0.0


def vertical_gap(left, right) -> float:
    if left.y1 < right.y0:
        return right.y0 - left.y1
    if right.y1 < left.y0:
        return left.y0 - right.y1
    return 0.0


def contained_in(inner, outer, *, share: float = 0.98) -> bool:
    """Whether ``share`` of ``inner``'s area lies inside ``outer``.

    A fraction rather than exact containment because both boxes are measured,
    not declared: a drawing cluster inside a printed figure sits a fraction of
    a point outside the region rendered for that figure, and a strict test
    reads that as a separate figure. It is also why the smaller box is the one
    tested -- two boxes that mutually contain 98% of each other are the same
    region twice, and the caller breaks that tie by size.
    """
    area = inner.get_area()
    if area <= 0:
        return False
    return (inner & outer).get_area() / area >= share
