"""What a rendered page measures out to, in one coordinate system.

Measurement reads the PDF the export already produced rather than the .pptx,
because a shape's declared box says nothing about where its wrapped text
actually lands: the predecessor estimated that from declared geometry plus font
and line-spacing assumptions and reported collisions on pages a render shows to
be clean. The rendered word is ground truth.

Two backends can produce these boxes -- PDFium through `pypdfium2`, or poppler's
`pdftotext -bbox` -- and they do not agree on axes, so the contract fixes one
convention and both adapters convert into it:

    unit    PDF points, 1/72 inch
    origin  top-left corner of the page, y growing downward

That is poppler's native output, and it is also the space a .pptx lives in once
EMU are divided by 12700, which is the whole point: a rule's position comes from
the .pptx where it is exact, the words come from the render where they are true,
and the two can be compared without a flip in the middle. PDFium reports text in
the other convention (origin bottom-left, y up) and its adapter flips it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageSize:
    """One rendered page's paper size, in points.

    Carried alongside the boxes because "y from the top" is only meaningful
    against a height, and anything that normalises a box -- a finding that says
    a word sits in the bottom third, a check that type stays inside the safe
    area -- needs the page it sat on.
    """

    width_pt: float
    height_pt: float


@dataclass(frozen=True)
class WordBox:
    """One rendered stretch of text and where it landed.

    A "word" is what the extractor calls one: a run of characters with no
    whitespace and no jump between them. For Latin copy that is a word; for CJK,
    which has no spaces, it is a whole line. Both are usable for what reads
    these -- text painted over text, type outside the safe area -- because the
    question is where a piece of copy sits, not where a lexical boundary is.
    """

    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def overlap(self, other: WordBox) -> float:
        """Shared area in square points; 0 when the two do not touch."""
        wide = min(self.x1, other.x1) - max(self.x0, other.x0)
        tall = min(self.y1, other.y1) - max(self.y0, other.y0)
        return wide * tall if wide > 0 and tall > 0 else 0.0
